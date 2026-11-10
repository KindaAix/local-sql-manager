"""操作历史页：列出审计记录并提供「回退」表单入口。

回退本身复用 services.undo；本模块只做页面渲染、事务边界与中文错误提示。
页面用普通表单提交（不依赖 JS），动作仍落在同一套撤销逻辑上。
"""

from __future__ import annotations

import logging
import sqlite3

from flask import Blueprint, flash, redirect, render_template, request, url_for

from local_sql_manager.app import get_conn
from local_sql_manager.services import audit, schema, undo
from local_sql_manager.services.models import Irreversible, StaleOperation

logger = logging.getLogger(__name__)

bp = Blueprint("history", __name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

KIND_LABELS = {
    "insert": "新增",
    "update": "修改",
    "delete": "删除",
    "restore": "恢复",
    "purge": "彻底删除",
    "import": "导入",
    "merge": "合并",
    "undo": "撤销",
    "create_table": "新建表",
    "rename_table": "重命名表",
    "drop_table": "删除表",
    "restore_table": "恢复表",
    "purge_table": "彻底删除表",
    "create_field": "新建字段",
    "rename_field": "重命名字段",
    "drop_field": "删除字段",
    "set_field_type": "修改字段类型",
    "view": "视图",
    "adopt": "启用编辑",
}

SOURCE_LABELS = {"web": "界面", "mcp": "智能助手", "system": "系统"}

VIEW_ACTIONS = {"create": "新建", "rename": "重命名", "delete": "删除"}


def _parse_limit(raw: object) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if value <= 0:
        return DEFAULT_LIMIT
    return min(value, MAX_LIMIT)


def _row_count(payload: dict) -> int:
    rows = payload.get("rows")
    return len(rows) if isinstance(rows, list) else 0


def _summarize(op: dict) -> str:
    """把 payload 压成一句中文摘要；认不出的 kind 留空，不抛异常。"""
    kind = op.get("kind")
    payload = op.get("payload") or {}
    if kind in ("insert", "update", "delete", "restore", "purge"):
        actions = {
            "insert": "新增",
            "update": "修改",
            "delete": "删除",
            "restore": "恢复",
            "purge": "彻底删除",
        }
        count = _row_count(payload)
        return f"{actions[kind]} {count} 行" if count else actions[kind]
    if kind == "import":
        tables = payload.get("tables") or []
        file = payload.get("file")
        text = f"导入 {len(tables)} 张表"
        return f"{text}（{file}）" if file else text
    if kind == "merge":
        tables = payload.get("tables") or []
        target = payload.get("target") or op.get("table")
        rows = payload.get("row_count")
        return f"合并 {len(tables)} 张表为「{target}」，共 {rows} 行"
    if kind == "undo":
        origin = payload.get("op")
        return f"回退操作 #{origin}" if origin is not None else "回退操作"
    if kind == "rename_table":
        return f"{payload.get('old')} → {payload.get('new')}"
    if kind == "drop_table":
        return "表移入回收站"
    if kind == "restore_table":
        return f"恢复为「{payload.get('restored_as') or op.get('table')}」"
    if kind in ("purge_table", "create_table"):
        return KIND_LABELS[kind]
    if kind in ("create_field", "drop_field", "set_field_type"):
        return f"{KIND_LABELS[kind]}「{payload.get('column') or ''}」"
    if kind == "rename_field":
        return f"字段 {payload.get('old')} → {payload.get('new')}"
    if kind == "view":
        action = VIEW_ACTIONS.get(payload.get("action"), "更新")
        return f"{action}视图「{payload.get('name') or ''}」"
    if kind == "adopt":
        added = payload.get("added_columns") or []
        return "启用编辑" + (f"，新增 {'、'.join(added)} 列" if added else "")
    return ""


def _present(op: dict) -> dict:
    item = dict(op)
    item["kind_label"] = KIND_LABELS.get(op["kind"], op["kind"])
    item["source_label"] = SOURCE_LABELS.get(op["source"], op["source"])
    item["summary"] = _summarize(op)
    item["undone"] = op.get("undone_by") is not None
    item["undoable"] = not item["undone"] and not op.get("irreversible")
    item["origin"] = (op.get("payload") or {}).get("op")
    return item


@bp.get("/history")
def history_page():
    conn = get_conn()
    tables = schema.list_tables(conn)
    table = request.args.get("table") or None
    if table not in tables:
        table = None
    limit = _parse_limit(request.args.get("limit"))
    operations = [_present(op) for op in audit.history(conn, table, limit)]
    return render_template(
        "history.html",
        tables=tables,
        table=table,
        limit=limit,
        operations=operations,
    )


@bp.post("/history/undo/<int:op_id>")
def undo_operation(op_id: int):
    conn = get_conn()
    table = request.form.get("table") or None
    limit = _parse_limit(request.form.get("limit"))
    try:
        undo.undo(conn, op_id, source="web")
        conn.commit()
    except Irreversible as exc:
        conn.rollback()
        flash(str(exc) or "该操作不可撤销。")
    except StaleOperation as exc:
        conn.rollback()
        flash(str(exc) or "该操作已过期，无法回退。")
    except ValueError as exc:
        conn.rollback()
        flash(str(exc) or "无法回退该操作。")
    except sqlite3.Error:
        conn.rollback()
        logger.exception("回退操作失败")
        flash("回退失败，请重试。")
    else:
        flash(f"已回退操作 #{op_id}。")
    return redirect(url_for("history.history_page", table=table, limit=limit))
