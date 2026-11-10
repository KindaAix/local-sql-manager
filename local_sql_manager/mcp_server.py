"""FastMCP 服务：把同一批数据以结构化工具交给 agent。

工具全部是薄封装：校验参数 → 调 services → 返回 JSON 友好的结构，不提供
任意 SQL。所有工具都用 library.path 新开一条短连接（sqlite 连接不能跨线程
共享），写完即 commit，异常先 rollback 再转成 ValueError / PermissionError，
避免把 sqlite3 原始异常漏给 agent。

写工具统一 source="mcp"，因此与界面写入走同一套审计、撤销与乐观锁。
"""

from __future__ import annotations

import hmac
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import parse_qs

from fastmcp import FastMCP

from .services import audit
from .services import fields as fields_service
from .services import merge as merge_service
from .services import query
from .services import rows as rows_service
from .services import schema
from .services import undo as undo_service
from .services import views
from .services.library import Library
from .services.models import ConflictError, Irreversible, StaleOperation, ViewConfig

logger = logging.getLogger(__name__)

SOURCE = "mcp"
CONNECT_TIMEOUT = 30
READ_ONLY_MESSAGE = "当前为只读模式，禁止写入"

INSTRUCTIONS = """这是「本地多维表格管理器」的数据接口，读写用户本机的一个 SQLite 库文件。

使用顺序：先 list_tables() 看有哪些表，再 get_schema(table) 看列与类型，
然后 get_rows(table) 或 count_rows(table, filters) 取数。写入用 create_rows、
update_rows、delete_rows；删除是软删（进回收站），可用 restore_rows 恢复。
每次写入都会记一条审计，返回的 op 是操作 id，可用 undo(op) 撤销上一步。

筛选条件形如 [{"column": "金额", "op": "gt", "value": 100}]，可用操作符
随类型不同：text/url 有 contains、not_contains、eq、neq、empty、not_empty；
number/date 另有 gt、gte、lt、lte；select 只有 eq、neq、empty、not_empty；
multiselect 用 contains；checkbox 用 is_true、is_false。

写入前建议先读一次行拿到 id 与 updated_at。update_rows 不传 expected 时会
自动读取当前版本，但读到写入之间若有别的写入发生仍会报冲突，需要重新读取。
"""


# --- 连接与错误处理 ---------------------------------------------------


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _connect(library: Library) -> sqlite3.Connection:
    conn = sqlite3.connect(library.path, timeout=CONNECT_TIMEOUT)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def _read_conn(library: Library):
    conn = _connect(library)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_conn(library: Library):
    """写连接：成功提交，失败回滚，最后一定关闭。"""
    conn = _connect(library)
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _conflict_message(exc: ConflictError) -> str:
    ids = []
    for row in exc.rows or []:
        if isinstance(row, dict) and row.get("id") is not None:
            ids.append(str(row["id"]))
    message = "该记录已被修改"
    if ids:
        shown = "、".join(ids[:3])
        if len(ids) > 3:
            shown += f" 等 {len(ids)} 行"
        message += f"（{shown}）"
    return message + "，请重新读取后再试"


@contextmanager
def _errors():
    """把服务层与 sqlite 的异常统一转成 agent 能读懂的 ValueError。"""
    try:
        yield
    except ConflictError as exc:
        raise ValueError(_conflict_message(exc)) from None
    except (StaleOperation, Irreversible) as exc:
        raise ValueError(str(exc) or "该操作不可撤销") from None
    except (PermissionError, ValueError):
        raise
    except sqlite3.OperationalError as exc:
        logger.debug("MCP 数据库错误：%r", exc)
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            raise ValueError("数据库正忙，请稍后重试") from None
        raise ValueError("数据库错误") from None
    except sqlite3.Error as exc:
        # 不把 sqlite 的英文原文漏给 agent，详情只进日志。
        logger.debug("MCP 数据库错误：%r", exc)
        raise ValueError("数据库错误") from None


def _require_table(conn: sqlite3.Connection, table: str) -> str:
    if not isinstance(table, str) or not table.strip():
        raise ValueError("表名必须是非空文本")
    name = table.strip()
    if name.lower().startswith(("__lsm_", "sqlite_")):
        raise ValueError(f"不能操作内部表：{name}")
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    if found is None:
        raise ValueError(f"表不存在：{name}")
    return name


def _as_id(value):
    """把 JSON 传来的字符串主键还原成整数；非数字主键保持原样。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def _as_config(config) -> ViewConfig:
    if isinstance(config, ViewConfig):
        return config
    if isinstance(config, dict):
        return ViewConfig(
            **{
                key: value
                for key, value in config.items()
                if key in ViewConfig.__dataclass_fields__
            }
        )
    raise ValueError("视图配置类型不正确")


def _default_view_config(conn: sqlite3.Connection, table: str) -> ViewConfig:
    """表的默认视图配置；没有视图时退回空配置（与 list_views 的内存默认视图一致）。"""
    for item in views.list_views(conn, table):
        if item.get("is_default"):
            return _as_config(item["config"])
    return ViewConfig()


def _check_filters(conn: sqlite3.Connection, table: str, filters) -> list:
    """逐条校验筛选条件；query.build_where 对未知项是静默忽略，MCP 不能这样。"""
    if filters is None:
        return []
    if not isinstance(filters, list):
        raise ValueError("filters 必须是筛选条件列表")
    by_name = {field.name: field for field in schema.get_fields(conn, table)}
    checked = []
    for item in filters:
        if not isinstance(item, dict):
            raise ValueError("筛选条件不支持：每一项都必须是对象")
        column = item.get("column")
        if not isinstance(column, str) or column not in by_name:
            raise ValueError(f"筛选条件不支持：未知列 {column}")
        op = item.get("op")
        if op not in query.OPS.get(by_name[column].type, ()):
            raise ValueError(f"筛选条件不支持：{column} 不支持操作符 {op}")
        value = item.get("value")
        if op not in ("empty", "not_empty", "is_true", "is_false") and (
            value is None or (isinstance(value, str) and not value.strip())
        ):
            # build_where 对空取值是静默跳过，这里必须报错，否则等于没筛。
            raise ValueError("筛选条件缺少取值")
        checked.append(item)
    return checked


def _as_id_list(value) -> list:
    """行 id 列表必须是 JSON 数组；字符串会被当成逐字符迭代，必须显式拒绝。"""
    if not isinstance(value, list):
        raise ValueError("ids 必须是行 id 列表")
    return [_as_id(item) for item in value]


def _reject_deleted(conn: sqlite3.Connection, table: str, ids: list) -> None:
    """已软删的行不能直接改写，先恢复再编辑，避免把回收站的数据改脏。"""
    if not ids:
        return
    columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({_quote(table)})")
    }
    if "deleted_at" not in columns:
        return
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f'SELECT "id" FROM {_quote(table)} WHERE "id" IN ({placeholders})'
        ' AND "deleted_at" IS NOT NULL',
        ids,
    ).fetchall()
    if rows:
        raise ValueError("该行已删除，请先恢复")


def _resolve_view(
    conn: sqlite3.Connection, table: str, view
) -> ViewConfig:
    """view 可为 None（用默认视图）、视图 id 或视图名称。"""
    if view is None:
        return _default_view_config(conn, table)
    catalog = views.list_views(conn, table)
    wanted = None
    if isinstance(view, bool):
        raise ValueError("视图参数不正确")
    if isinstance(view, int):
        wanted = view
    elif isinstance(view, str) and view.strip().isdigit():
        wanted = int(view.strip())
    if wanted is not None:
        for item in catalog:
            if item["id"] == wanted:
                return _as_config(item["config"])
        raise ValueError(f"视图不存在：{view}")
    name = str(view).strip()
    for item in catalog:
        if item["name"] == name:
            return _as_config(item["config"])
    raise ValueError(f"视图不存在：{view}")


def _page_dict(result) -> dict:
    return {
        "rows": list(result.rows),
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
        "groups": [
            {
                "key": group.key,
                "count": group.count,
                "aggregates": list(group.aggregates),
            }
            for group in result.groups
        ],
    }


# --- Token 鉴权 -------------------------------------------------------

UNAUTHORIZED_TEXT = "未授权的 MCP 请求"


def _header_value(scope: dict, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _query_token(scope: dict) -> str:
    raw = scope.get("query_string") or b""
    values = parse_qs(raw.decode("latin-1")).get("token") or []
    return values[0] if values else ""


class TokenAuthMiddleware:
    """ASGI 中间件：校验 Authorization: Bearer <token> 或 ?token=<token>。

    放在 FastMCP 生成的 ASGI 应用外面，不改 FastMCP 内部实现；非 HTTP 的
    lifespan 等 scope 原样透传。
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind != "http":
            # MCP 只用 http 与 lifespan；websocket 等一律不转给内部应用。
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        header = _header_value(scope, b"authorization").strip()
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = _query_token(scope)
        # compare_digest 的 str 版本遇到非 ASCII 会抛 TypeError（变成 500），
        # 统一 encode 成 bytes 再比。
        if not self.token or not hmac.compare_digest(
            supplied.encode("utf-8"), self.token.encode("utf-8")
        ):
            body = UNAUTHORIZED_TEXT.encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def make_http_app(mcp: FastMCP, path: str = "/mcp"):
    """FastMCP 的 ASGI 应用；build_mcp 传了 token 时套上鉴权中间件。"""
    app = mcp.http_app(path=path)
    token = getattr(mcp, "lsm_token", None)
    if token is not None:
        # 空 token 也要套中间件（一律 401），绝不能因为没有 token 就放行。
        return TokenAuthMiddleware(app, token)
    return app


# --- FastMCP 实例与工具 -----------------------------------------------


def build_mcp(
    library: Library, read_only: bool = False, token: str | None = None
) -> FastMCP:
    """构造绑定到某个库的 FastMCP 实例。

    read_only=True 时所有写工具都抛 PermissionError，读工具照常可用。
    token 交给 make_http_app 套成 ASGI 鉴权中间件。
    """
    mcp = FastMCP(name="local-sql-manager", instructions=INSTRUCTIONS)
    mcp.lsm_token = token

    def guard() -> None:
        if read_only:
            raise PermissionError(READ_ONLY_MESSAGE)

    @mcp.tool
    def list_libraries() -> list[dict]:
        """列出可访问的库。当前只支持单库，返回正在使用的这个库。"""
        path = Path(library.path)
        return [{"path": str(path), "name": path.stem, "current": True}]

    @mcp.tool
    def list_tables() -> list[str]:
        """列出库里所有用户表（不包含内部元数据表）。"""
        with _errors(), _read_conn(library) as conn:
            return schema.list_tables(conn)

    @mcp.tool
    def get_schema(table: str) -> list[dict]:
        """返回一张表的字段定义：名称、逻辑类型、选项、顺序与列宽。"""
        with _errors(), _read_conn(library) as conn:
            name = _require_table(conn, table)
            return [
                {
                    "name": field.name,
                    "type": field.type,
                    "options": list(field.options),
                    "position": field.position,
                    "width": field.width,
                }
                for field in schema.get_fields(conn, name)
            ]

    @mcp.tool
    def list_views(table: str) -> list[dict]:
        """列出表的视图；没有落库视图时返回内存中的默认视图「全部记录」。"""
        with _errors(), _read_conn(library) as conn:
            name = _require_table(conn, table)
            return [
                {
                    "id": item["id"],
                    "table": item["table"],
                    "name": item["name"],
                    "config": asdict(_as_config(item["config"])),
                    "position": item["position"],
                    "is_default": item["is_default"],
                }
                for item in views.list_views(conn, name)
            ]

    @mcp.tool
    def get_rows(
        table: str, view: int | str | None = None, page: int = 1, page_size: int = 100
    ) -> dict:
        """按视图读一页行。

        view 可传视图 id 或名称，不传则用默认视图。返回
        {"rows", "total", "page", "page_size", "groups"}，total 与 groups
        按整个筛选集统计。page_size 上限 1000。
        """
        with _errors(), _read_conn(library) as conn:
            name = _require_table(conn, table)
            config = _resolve_view(conn, name, view)
            result = rows_service.get_page(conn, name, config, page, page_size)
            return _page_dict(result)

    @mcp.tool
    def count_rows(table: str, filters: list[dict] | None = None) -> int:
        """统计未删除的行数，按默认视图的筛选叠加 filters。

        filters 形如 [{"column": "金额", "op": "gt", "value": 100}]；
        未知列或该类型不支持的操作符直接报错，不会静默返回全表。
        """
        with _errors(), _read_conn(library) as conn:
            name = _require_table(conn, table)
            extra = _check_filters(conn, name, filters)
            view = _default_view_config(conn, name)
            fields = schema.get_fields(conn, name)
            where, params = query.build_where(
                list(view.filters or []) + extra, fields
            )
            conditions = []
            columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({_quote(name)})")
            }
            if "deleted_at" in columns:
                conditions.append('"deleted_at" IS NULL')
            if where:
                conditions.append(f"({where})")
            sql = f"SELECT COUNT(*) FROM {_quote(name)}"
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            return int(conn.execute(sql, params).fetchone()[0])

    @mcp.tool
    def create_rows(table: str, rows: list[dict]) -> dict:
        """插入若干行，返回 {"op": 操作 id, "ids": 新行 id 列表}。

        rows 是对象列表，键为列名。
        """
        guard()
        if not isinstance(rows, list):
            raise ValueError("rows 必须是对象列表")
        with _errors(), _write_conn(library) as conn:
            name = _require_table(conn, table)
            op = rows_service.insert_rows(conn, name, rows, source=SOURCE)
            if op is None:
                return {"op": None, "ids": []}
            recorded = audit.get_op(conn, op) or {}
            payload = recorded.get("payload") or {}
            ids = [
                row.get("id")
                for row in payload.get("rows") or []
                if isinstance(row, dict)
            ]
            return {"op": op, "ids": ids}

    @mcp.tool
    def update_rows(
        table: str,
        changes: list[dict],
        expected: dict[str, str] | None = None,
    ) -> dict:
        """按行更新若干单元格，返回 {"op": 操作 id}。

        changes 形如 [{"id": 1, "金额": 77}]；expected 形如
        {"1": "2026-11-10T08:00:00Z"}，键为行 id、值为读取时该行的
        updated_at。不传 expected 时会在写入前自动读取这些行的当前
        updated_at 作为版本，因此仍可能与同时发生的写入冲突（抛「已被修改」），
        冲突后需重新读取再写。
        """
        guard()
        if not isinstance(changes, list):
            raise ValueError("changes 必须是对象列表")
        mapping: dict = {}
        for item in changes:
            if not isinstance(item, dict) or item.get("id") is None:
                raise ValueError("changes 的每一项都必须是带 id 的对象")
            rid = _as_id(item["id"])
            values = {
                key: value for key, value in item.items() if key != "id"
            }
            mapping.setdefault(rid, {}).update(values)
        if expected is not None and not isinstance(expected, dict):
            raise ValueError("expected 必须是 id 到 updated_at 的对象")

        with _errors(), _write_conn(library) as conn:
            name = _require_table(conn, table)
            _reject_deleted(conn, name, list(mapping))
            versions = None
            if expected is None:
                versions = _current_versions(conn, name, list(mapping))
            else:
                versions = {_as_id(key): value for key, value in expected.items()}
            op = rows_service.update_cells(
                conn, name, mapping, expected=versions, source=SOURCE
            )
            return {"op": op}

    @mcp.tool
    def delete_rows(table: str, ids: list[int]) -> dict:
        """把若干行移入回收站（软删），返回 {"op": 操作 id}。"""
        guard()
        wanted = _as_id_list(ids)
        with _errors(), _write_conn(library) as conn:
            name = _require_table(conn, table)
            op = rows_service.soft_delete(conn, name, wanted, source=SOURCE)
            return {"op": op}

    @mcp.tool
    def restore_rows(table: str, ids: list[int]) -> dict:
        """把回收站里的行恢复，返回 {"op": 操作 id}。"""
        guard()
        wanted = _as_id_list(ids)
        with _errors(), _write_conn(library) as conn:
            name = _require_table(conn, table)
            op = rows_service.restore_rows(conn, name, wanted, source=SOURCE)
            return {"op": op}

    @mcp.tool
    def create_table(table: str, columns: list[dict]) -> str:
        """新建一张表并返回表名。

        columns 形如 [{"name": "金额", "type": "number", "options": []}]；
        type 只能是 text/number/date/checkbox/select/multiselect/url，
        select 与 multiselect 的选项放在 options 里。
        """
        guard()
        name = table.strip() if isinstance(table, str) else ""
        if not name:
            raise ValueError("表名不能为空")
        if not isinstance(columns, list) or not columns:
            raise ValueError("columns 必须是至少含一个字段的对象列表")
        specs = []
        for item in columns:
            if not isinstance(item, dict) or not item.get("name"):
                raise ValueError("columns 的每一项都必须带 name")
            specs.append(
                schema.ColumnSpec(
                    str(item["name"]),
                    str(item.get("type") or "text"),
                    tuple(item.get("options") or ()),
                )
            )
        with _errors(), _write_conn(library) as conn:
            # 重名先自己判掉，别让 sqlite 的英文原文漏出去。
            if schema._name_taken(conn, name):
                raise ValueError(f"表已存在：{name}")
            return schema.create_table(conn, name, specs)

    @mcp.tool
    def add_field(
        table: str,
        name: str,
        type: str,
        options: list[str] | None = None,
        after: str | None = None,
    ) -> dict:
        """给表新增一列，返回 {"op": 操作 id}。

        after 指定插在哪个已有列之后，不传则追加到末尾。
        """
        guard()
        with _errors(), _write_conn(library) as conn:
            spec = schema.ColumnSpec(str(name), str(type), tuple(options or ()))
            op = fields_service.add_field(
                conn, table, spec, after=after, source=SOURCE
            )
            return {"op": op}

    @mcp.tool
    def merge_tables(
        tables: list[str],
        target: str,
        mapping: dict[str, dict[str, str]] | None = None,
    ) -> dict:
        """把多张表合并进一张新表，返回 {"op": 操作 id}。

        mapping 形如 {"目标列": {"源表名": "源列名"}}，顺序即目标列顺序；
        不传则按列名取并集。
        """
        guard()
        if not isinstance(tables, list):
            raise ValueError("tables 必须是表名列表")
        with _errors(), _write_conn(library) as conn:
            op = merge_service.merge_tables(
                conn, tables, target, mapping=mapping, source=SOURCE
            )
            return {"op": op}

    @mcp.tool
    def get_history(table: str | None = None, limit: int = 50) -> list[dict]:
        """按时间倒序返回操作历史，可按表名筛选；limit 上限 500。"""
        with _errors(), _read_conn(library) as conn:
            if table is not None:
                # 已删的表也要能查历史，这里只做参数校验，不校验表是否存在。
                if not isinstance(table, str) or not table.strip():
                    raise ValueError("表名必须是非空文本")
                table = table.strip()
            return audit.history(conn, table, limit)

    @mcp.tool
    def undo(op_id: int) -> dict:
        """撤销一条操作，返回 {"op": 新撤销操作的 id}。

        目标操作已被撤销、被后续写入影响或本身不可撤销时抛 ValueError。
        """
        guard()
        with _errors(), _write_conn(library) as conn:
            new_op = undo_service.undo(conn, op_id, source=SOURCE)
            return {"op": new_op}

    return mcp


def _current_versions(
    conn: sqlite3.Connection, table: str, ids: list
) -> dict:
    """读取这些行当前的 updated_at，作为未传 expected 时的版本基线。"""
    if not ids:
        return {}
    columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({_quote(table)})")
    }
    if "updated_at" not in columns:
        raise ValueError(f"表 {table} 没有 updated_at 列，无法启用并发保护")
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f'SELECT "id", "updated_at" FROM {_quote(table)}'
        f' WHERE "id" IN ({placeholders})',
        ids,
    ).fetchall()
    versions = {row[0]: row[1] for row in rows}
    missing = [rid for rid in ids if rid not in versions]
    if missing:
        raise ValueError(f"行不存在：{missing[0]}")
    return versions
