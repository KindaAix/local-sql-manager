"""FastMCP 服务：把同一批数据以结构化工具交给 agent。

工具全部是薄封装：校验参数 → 调 services → 返回 JSON 友好的结构，不提供
任意 SQL。所有工具都用 library.path 新开一条短连接（sqlite 连接不能跨线程
共享），写完即 commit，异常先 rollback 再转成 ValueError / PermissionError，
避免把 sqlite3 原始异常漏给 agent。

写工具统一 source="mcp"，因此与界面写入走同一套审计、撤销与乐观锁。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

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
    except sqlite3.Error as exc:
        logger.debug("MCP 数据库错误：%r", exc)
        raise ValueError(f"数据库错误：{exc}") from None


def _require_table(conn: sqlite3.Connection, table: str) -> str:
    if not isinstance(table, str) or not table.strip():
        raise ValueError("表名必须是非空文本")
    name = table.strip()
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


def _resolve_view(
    conn: sqlite3.Connection, table: str, view
) -> ViewConfig:
    """view 可为 None、视图 id 或视图名称。"""
    if view is None:
        return ViewConfig()
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


# --- FastMCP 实例与工具 -----------------------------------------------


def build_mcp(library: Library, read_only: bool = False) -> FastMCP:
    """构造绑定到某个库的 FastMCP 实例。

    read_only=True 时所有写工具都抛 PermissionError，读工具照常可用。
    """
    mcp = FastMCP(name="local-sql-manager", instructions=INSTRUCTIONS)

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
        """统计未删除的行数，可按筛选条件过滤。

        filters 形如 [{"column": "金额", "op": "gt", "value": 100}]。
        """
        with _errors(), _read_conn(library) as conn:
            name = _require_table(conn, table)
            if filters is not None and not isinstance(filters, list):
                raise ValueError("filters 必须是筛选条件列表")
            fields = schema.get_fields(conn, name)
            where, params = query.build_where(filters or [], fields)
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
        with _errors(), _write_conn(library) as conn:
            name = _require_table(conn, table)
            op = rows_service.soft_delete(conn, name, list(ids or []), source=SOURCE)
            return {"op": op}

    @mcp.tool
    def restore_rows(table: str, ids: list[int]) -> dict:
        """把回收站里的行恢复，返回 {"op": 操作 id}。"""
        guard()
        with _errors(), _write_conn(library) as conn:
            name = _require_table(conn, table)
            op = rows_service.restore_rows(conn, name, list(ids or []), source=SOURCE)
            return {"op": op}

    @mcp.tool
    def create_table(table: str, columns: list[dict]) -> str:
        """新建一张表并返回表名。

        columns 形如 [{"name": "金额", "type": "number", "options": []}]；
        type 只能是 text/number/date/checkbox/select/multiselect/url，
        select 与 multiselect 的选项放在 options 里。
        """
        guard()
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
            return schema.create_table(conn, str(table).strip(), specs)

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
                table = _require_table(conn, table)
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
