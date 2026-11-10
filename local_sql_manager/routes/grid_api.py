"""网格写入 API：单元格保存、新增、批量删除、撤销与视图列配置。

本模块只做参数校验、事务边界与错误映射，数据规则全部交给 services。
约定：

- 所有 POST 都要求 `Content-Type: application/json`，否则 415；
- 表必须先出现在 `schema.list_tables` 里，内部表与不存在的表一律 404；
- 成功路径显式 `commit`，失败路径显式 `rollback`，不依赖 teardown；
- 保存与新增返回可直接替换的 HTML 行片段（text/html），其余返回 JSON。
"""

from __future__ import annotations

import base64
import logging
import sqlite3

from flask import Blueprint, Response, jsonify, render_template, request

from local_sql_manager.app import get_conn
from local_sql_manager.services import audit, rows, schema, undo, views
from local_sql_manager.services.models import (
    ConflictError,
    Irreversible,
    StaleOperation,
    ViewConfig,
)

from .grid import _business_fields, _display_columns, _select_view

logger = logging.getLogger(__name__)

bp = Blueprint("grid_api", __name__, url_prefix="/api")

MIN_COLUMN_WIDTH = 40
MAX_COLUMN_WIDTH = 800


class _ApiError(Exception):
    """带 HTTP 状态码的可读错误，路由内统一映射成 {"error": ...}。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _fail(message: str, status: int) -> tuple:
    return jsonify({"error": message}), status


def _json_body() -> dict:
    """读取 JSON 请求体；非 JSON 415，非对象 400。"""
    if not request.is_json:
        raise _ApiError("请求格式必须是 JSON", 415)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise _ApiError("请求体必须是 JSON 对象")
    return data


def _require_table(conn: sqlite3.Connection, table: str) -> None:
    if table not in schema.list_tables(conn):
        raise _ApiError(f"表不存在：{table}", 404)


def _require_managed(conn: sqlite3.Connection, table: str) -> None:
    """只有登记在 __lsm_table 且状态为 active 的表才允许写入。

    未登记的外部表虽然出现在 list_tables 里，但没有字段元数据、视图与
    回收站语义，写入会留下审计/回收站都看不见的孤儿数据，所以直接拒绝。
    """
    row = conn.execute(
        'SELECT "status" FROM "__lsm_table" WHERE "table_name" = ?', (table,)
    ).fetchone()
    if row is None or row[0] != "active":
        raise _ApiError("该表未启用编辑，请先启用编辑", 403)


def _physical_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cursor = conn.execute(f"PRAGMA table_info({_quote(table)})")
    return [row[1] for row in cursor.fetchall()]


def _as_id(value: object) -> object:
    """与行服务保持一致的宽松主键转换：能转整数就转，转不了保留原值。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def _fetch_row(conn: sqlite3.Connection, table: str, row_id: object) -> dict | None:
    """按主键取整行；表没有 id 列时退回 rowid（外部表兜底）。"""
    columns = _physical_columns(conn, table)
    key = '"id"' if "id" in columns else "rowid"
    cursor = conn.execute(
        f"SELECT * FROM {_quote(table)} WHERE {key} = ?", (row_id,)
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _display_columns_for(conn: sqlite3.Connection, table: str) -> list:
    """按当前请求的视图参数取列，保证返回的片段与页面列序一致。"""
    options = views.list_views(conn, table)
    active = _select_view(options, request.args.get("view"))
    config = views.load_config(conn, active["id"])
    return _display_columns(conn, table, _business_fields(conn, table), config)


def _rows_html(conn: sqlite3.Connection, table: str, row_ids: list) -> str:
    """渲染若干行片段；调用前必须已经 commit，保证读到的就是新值。"""
    columns = _display_columns_for(conn, table)
    parts = []
    for row_id in row_ids:
        row = _fetch_row(conn, table, row_id)
        if row is None:
            continue
        parts.append(
            render_template("_grid_row.html", row=row, columns=columns)
        )
    return "".join(parts)


def _safe_value(value: object):
    """冲突行会直接进 JSON，把 bytes 之类换成可序列化的形式。"""
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value)


def _conflict_row(row: dict) -> dict:
    return {key: _safe_value(value) for key, value in (row or {}).items()}


def _integrity_message(exc: sqlite3.IntegrityError) -> str:
    """把 SQLite 的约束错误换成带列名的中文提示。"""
    raw = str(exc)
    detail = raw.split(":", 1)[1].strip() if ":" in raw else ""
    column = detail.rsplit(".", 1)[-1].strip() if "." in detail else ""
    if "NOT NULL" in raw and column:
        return f"「{column}」不能为空"
    if "UNIQUE" in raw and column:
        return f"「{column}」已存在重复值"
    return "数据不满足约束，请检查必填项与唯一值"


def _friendly_message(message: str) -> str:
    """服务层的类型转换异常可能带 Python 原文，这里换成界面可读的中文。"""
    if message.startswith(
        ("could not convert string to float", "invalid literal for int")
    ):
        return "请输入有效的数字"
    return message


def _error_response(exc: BaseException):
    """服务层异常 → 统一 JSON 错误；不认识的异常返回 None 交给 Flask。"""
    if isinstance(exc, _ApiError):
        return _fail(exc.message, exc.status)
    if isinstance(exc, ConflictError):
        return (
            jsonify(
                {
                    "error": "该记录已被修改，请重新加载",
                    "conflict": [_conflict_row(row) for row in exc.rows],
                }
            ),
            409,
        )
    if isinstance(exc, (StaleOperation, Irreversible)):
        message = str(exc) or "该操作无法撤销"
        return _fail(message, 409)
    if isinstance(exc, ValueError):
        message = _friendly_message(str(exc))
        status = 404 if message.startswith(("视图不存在", "表不存在")) else 400
        return _fail(message, status)
    if isinstance(exc, sqlite3.Error):
        logger.exception("网格写入失败")
        return _fail("写入失败，请重试", 500)
    return None


def _run(write, success):
    """执行一次写事务：成功 commit 并渲染响应，失败 rollback 并映射错误。

    服务层函数只负责写、不 commit；这里用 try/except/else 把事务边界写死，
    不依赖 teardown 的隐式回滚。
    """
    conn = get_conn()
    try:
        result = write(conn)
    except Exception as exc:  # noqa: BLE001  统一按服务层异常映射
        conn.rollback()
        response = _error_response(exc)
        if response is None:
            raise
        return response
    else:
        try:
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            logger.exception("网格写入提交失败")
            return _error_response(exc)
        return success(conn, result)


@bp.post("/t/<path:table>/save")
def save_cells(table: str):
    """保存一个或多个行的若干单元格，返回被改行片段（每行一个 <tr>）。"""

    def write(conn):
        body = _json_body()
        _require_table(conn, table)
        _require_managed(conn, table)
        changes = body.get("changes")
        expected = body.get("expected")
        if not isinstance(changes, dict) or not changes:
            raise _ApiError("没有需要保存的修改")
        if not isinstance(expected, dict):
            raise _ApiError("缺少 updated_at 版本，无法安全写入")
        for values in changes.values():
            if not isinstance(values, dict):
                raise _ApiError("changes 的值必须是 {列: 值} 对象")
        op = rows.update_cells(
            conn, table, changes, expected, source="web"
        )
        if op is None:
            raise _ApiError("没有需要保存的修改")
        return [_as_id(key) for key in changes]

    def success(conn, row_ids):
        return Response(
            _rows_html(conn, table, row_ids), mimetype="text/html"
        )

    return _run(write, success)


@bp.post("/t/<path:table>/insert")
def insert_row(table: str):
    """新增一行（values 可为空对象），返回含 data-id 的新行片段。"""

    def write(conn):
        body = _json_body()
        _require_table(conn, table)
        _require_managed(conn, table)
        values = body.get("values", {})
        if values is None:
            values = {}
        if not isinstance(values, dict):
            raise _ApiError("values 必须是 {列: 值} 对象")
        try:
            op = rows.insert_rows(conn, table, [values], source="web")
        except sqlite3.IntegrityError as exc:
            raise _ApiError(_integrity_message(exc)) from exc
        if op is None:
            raise _ApiError("没有可新增的内容")
        record = audit.get_op(conn, op)
        payload_rows = (record or {}).get("payload", {}).get("rows") or []
        if not payload_rows:
            raise _ApiError("新增失败，请重试", 500)
        return payload_rows[0].get("after", {}).get("id")

    def success(conn, row_id):
        return Response(_rows_html(conn, table, [row_id]), mimetype="text/html")

    return _run(write, success)


@bp.post("/t/<path:table>/delete")
def delete_rows(table: str):
    """批量软删除，返回审计操作 id 与受影响行数。"""

    def write(conn):
        body = _json_body()
        _require_table(conn, table)
        _require_managed(conn, table)
        ids = body.get("ids")
        if not isinstance(ids, list):
            raise _ApiError("ids 必须是数组")
        if not ids:
            raise _ApiError("没有选择要删除的行")
        for value in ids:
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise _ApiError("行 id 必须是整数或字符串")
        op = rows.soft_delete(conn, table, ids, source="web")
        if op is None:
            return {"op": None, "deleted": 0}
        record = audit.get_op(conn, op)
        payload_rows = (record or {}).get("payload", {}).get("rows") or []
        return {"op": op, "deleted": len(payload_rows)}

    return _run(write, lambda conn, result: jsonify(result))


@bp.post("/undo/<int:op_id>")
def undo_op(op_id: int):
    """撤销一条操作；不存在的操作用 404，不可撤销/已过期用 409。"""

    def write(conn):
        _json_body()
        if audit.get_op(conn, op_id) is None:
            raise _ApiError(f"操作不存在：{op_id}", 404)
        return undo.undo(conn, op_id, source="web")

    return _run(
        write,
        lambda conn, new_op: jsonify({"op": new_op, "undone": op_id}),
    )


def _normalize_widths(raw: object) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _ApiError("widths 必须是 {列: 像素} 对象")
    widths = {}
    for name, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _ApiError(f"列宽必须是数字：{name}")
        width = int(value)
        if width < MIN_COLUMN_WIDTH or width > MAX_COLUMN_WIDTH:
            raise _ApiError(
                f"列宽需在 {MIN_COLUMN_WIDTH}–{MAX_COLUMN_WIDTH} 像素之间：{name}"
            )
        widths[str(name)] = width
    return widths


def _normalize_order(raw: object) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(
        not isinstance(name, str) for name in raw
    ):
        raise _ApiError("order 必须是列名数组")
    return list(raw)


@bp.post("/views/<int:view_id>/config")
def save_view_config(view_id: int):
    """合并保存列宽与列序；只接受已落库的视图（默认视图不可保存）。"""

    def write(conn):
        body = _json_body()
        if view_id == 0:
            raise _ApiError("默认视图不能保存列配置，请先新建视图")
        if views._fetch(conn, view_id) is None:
            raise _ApiError("视图不存在", 404)
        current = views.load_config(conn, view_id)
        widths = _normalize_widths(body.get("widths"))
        order = _normalize_order(body.get("order"))
        merged_widths = dict(current.widths or {})
        merged_widths.update(widths)
        config = ViewConfig(
            filters=list(current.filters or []),
            sorts=list(current.sorts or []),
            hidden=list(current.hidden or []),
            order=order or list(current.order or []),
            widths=merged_widths,
            group=current.group,
            aggregates=list(current.aggregates or []),
        )
        views.update_view(conn, view_id, config)
        return view_id

    return _run(write, lambda conn, _view: jsonify({"ok": True}))
