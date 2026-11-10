"""合并与外部表启用编辑：两个引导式工具页。

- 合并：选 2 张以上受管表与目标表名 → 预览列对齐/冲突 → 确认写入；
- 启用编辑：为未登记的外部表补系统列与元数据，成功后即可在网格里编辑。

服务层负责全部数据规则，这里只做参数转换、事务边界与中文错误提示。
"""

from __future__ import annotations

import logging
import sqlite3

from flask import Blueprint, flash, redirect, render_template, request, url_for

from local_sql_manager.app import get_conn
from local_sql_manager.services import adopt, merge, schema
from local_sql_manager.services.models import AdoptError

from .import_export import TYPE_LABELS

logger = logging.getLogger(__name__)

bp = Blueprint("tools", __name__)


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _managed_tables(conn: sqlite3.Connection) -> list[dict]:
    """合并只在受管表之间进行；每项带上行数方便界面展示。"""
    items = []
    for table in schema.list_tables(conn):
        fields = schema.get_fields(conn, table)
        if not fields:
            continue
        count = conn.execute(
            f"SELECT COUNT(*) FROM {_quote(table)} WHERE \"deleted_at\" IS NULL"
        ).fetchone()[0]
        items.append({"name": table, "fields": fields, "rows": count})
    return items


def _is_managed(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        'SELECT "status" FROM "__lsm_table" WHERE "table_name" = ?', (table,)
    ).fetchone()
    return row is not None and row[0] == "active"


# --- 合并 -------------------------------------------------------------


@bp.get("/merge")
def merge_page():
    conn = get_conn()
    tables = _managed_tables(conn)
    return render_template(
        "merge.html",
        tables=tables,
        plan=None,
        selected=request.args.getlist("tables"),
        target=request.args.get("target") or "",
        type_labels=TYPE_LABELS,
        error=None,
    )


@bp.post("/merge/preview")
def merge_preview():
    conn = get_conn()
    tables = _managed_tables(conn)
    selected = request.form.getlist("tables")
    target = (request.form.get("target") or "").strip()
    try:
        plan = merge.plan_merge(conn, selected, target)
    except ValueError as exc:
        return (
            render_template(
                "merge.html",
                tables=tables,
                plan=None,
                selected=selected,
                target=target,
                type_labels=TYPE_LABELS,
                error=str(exc) or "合并参数不正确",
            ),
            400,
        )
    return render_template(
        "merge.html",
        tables=tables,
        plan=plan,
        selected=selected,
        target=target,
        type_labels=TYPE_LABELS,
        error=None,
    )


@bp.post("/merge/confirm")
def merge_confirm():
    conn = get_conn()
    selected = request.form.getlist("tables")
    target = (request.form.get("target") or "").strip()
    try:
        merge.merge_tables(conn, selected, target)
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        flash(str(exc) or "合并失败，请检查选中的表与目标表名。")
        return redirect(url_for("tools.merge_page"))
    except sqlite3.Error:
        conn.rollback()
        logger.exception("合并失败")
        flash("合并失败，请重试。")
        return redirect(url_for("tools.merge_page"))
    flash(f"已合并为「{target}」。")
    return redirect(url_for("grid.table_view", table=target))


# --- 启用编辑 ---------------------------------------------------------


@bp.get("/adopt/<path:table>")
def adopt_page(table: str):
    conn = get_conn()
    if table not in schema.list_tables(conn):
        flash(f"表「{table}」不存在，可能已被删除或改名。")
        return redirect(url_for("grid.index"))
    if _is_managed(conn, table):
        flash(f"表「{table}」已启用编辑。")
        return redirect(url_for("grid.table_view", table=table))

    ok, reason = adopt.check_adoptable(conn, table)
    columns = []
    for row in conn.execute(f"PRAGMA table_info({_quote(table)})"):
        columns.append(
            {
                "name": row[1],
                "type": row[2] or "无类型",
                "pk": bool(row[5]),
                "required": bool(row[3]),
            }
        )
    return render_template(
        "adopt.html",
        table=table,
        ok=ok,
        reason=reason,
        steps=[part for part in reason.split("；") if part],
        columns=columns,
    )


@bp.post("/adopt/<path:table>/confirm")
def adopt_confirm(table: str):
    conn = get_conn()
    if table not in schema.list_tables(conn):
        flash(f"表「{table}」不存在，可能已被删除或改名。")
        return redirect(url_for("grid.index"))
    try:
        adopt.adopt_table(conn, table)
        conn.commit()
    except AdoptError as exc:
        conn.rollback()
        flash(str(exc) or "该表暂不支持启用编辑。")
        return redirect(url_for("tools.adopt_page", table=table))
    except (ValueError, sqlite3.Error):
        conn.rollback()
        logger.exception("启用编辑失败")
        flash("启用编辑失败，请重试。")
        return redirect(url_for("tools.adopt_page", table=table))
    flash(f"表「{table}」已启用编辑。")
    return redirect(url_for("grid.table_view", table=table))
