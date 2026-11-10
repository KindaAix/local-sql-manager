"""回收站：软删行与已删除表的恢复 / 彻底删除。

页面用普通表单提交，接口同时接受 JSON（MCP 之外的脚本与测试用）与表单
字段；彻底删除在页面上做二次确认，服务层负责不可逆审计。
"""

from __future__ import annotations

import logging
import sqlite3

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from local_sql_manager.app import get_conn
from local_sql_manager.services import audit, rows, schema

logger = logging.getLogger(__name__)

bp = Blueprint("trash", __name__)

ROW_LIMIT = 100
PREVIEW_COLUMNS = 3


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _is_managed(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        'SELECT "status" FROM "__lsm_table" WHERE "table_name" = ?', (table,)
    ).fetchone()
    return row is not None and row[0] == "active"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in [
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote(table)})")
    ]


@bp.get("/trash")
def trash_page():
    conn = get_conn()
    fields_by_table = {}
    groups = []
    for table in schema.list_tables(conn):
        if not _is_managed(conn, table) or not _has_column(conn, table, "deleted_at"):
            continue
        fields = schema.get_fields(conn, table)
        fields_by_table[table] = fields
        total = conn.execute(
            f'SELECT COUNT(*) FROM {_quote(table)} WHERE "deleted_at" IS NOT NULL'
        ).fetchone()[0]
        if not total:
            continue
        shown = fields[:PREVIEW_COLUMNS]
        cursor = conn.execute(
            f'SELECT * FROM {_quote(table)} WHERE "deleted_at" IS NOT NULL'
            ' ORDER BY "deleted_at" DESC, "id" DESC LIMIT ?',
            (ROW_LIMIT,),
        )
        items = []
        for raw in cursor.fetchall():
            row = dict(raw)
            items.append(
                {
                    "id": row.get("id"),
                    "deleted_at": row.get("deleted_at"),
                    "cells": [
                        {"name": field.name, "value": row.get(field.name)}
                        for field in shown
                    ],
                }
            )
        groups.append(
            {
                "table": table,
                "total": total,
                "entries": items,
                "shown": len(items),
                "columns": [field.name for field in shown],
            }
        )

    trashed_tables = schema.list_trashed_tables(conn)
    counts: dict[str, int] = {}
    for item in trashed_tables:
        counts[item["name"]] = counts.get(item["name"], 0) + 1
    for item in trashed_tables:
        # 同名多张时按名字无法唯一定位；恢复会自动加后缀，彻底删除则禁止。
        item["duplicated"] = counts[item["name"]] > 1
    return render_template(
        "trash.html",
        groups=groups,
        trashed_tables=trashed_tables,
        has_duplicate_tables=any(item["duplicated"] for item in trashed_tables),
    )


# --- 请求与响应 -------------------------------------------------------


def _payload() -> dict:
    """同时接受 JSON 与表单；表单的 ids 可以重复出现。"""
    if request.is_json:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body
    body = {key: value for key, value in request.form.items() if key != "ids"}
    ids = request.form.getlist("ids")
    if not ids:
        single = request.form.get("id")
        if single:
            ids = [single]
    body["ids"] = ids
    return body


def _ids(raw: object) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("ids 必须是数组")
    values = []
    for item in raw:
        if isinstance(item, bool):
            raise ValueError("行 id 必须是整数或字符串")
        if isinstance(item, int):
            values.append(item)
            continue
        text = str(item).strip()
        if not text:
            continue
        try:
            values.append(int(text))
        except ValueError:
            values.append(text)
    if not values:
        raise ValueError("没有选择要处理的行")
    return values


def _require_rows_table(conn: sqlite3.Connection, table: object) -> str:
    name = str(table or "").strip()
    if not name:
        raise ValueError("请指定表名")
    if name not in schema.list_tables(conn):
        raise ValueError(f"表不存在：{name}")
    if not _is_managed(conn, name):
        raise ValueError("该表未启用编辑，不能操作回收站")
    if not _has_column(conn, name, "deleted_at"):
        raise ValueError("该表没有回收站")
    return name


def _require_table_name(raw: object) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ValueError("请指定表名")
    return name


def _after_write(message: str, payload: dict | None = None):
    if request.is_json:
        return jsonify({"ok": True, "message": message, **(payload or {})})
    flash(message)
    return redirect(url_for("trash.trash_page"))


def _api_error(message: str, status: int):
    if request.is_json:
        return jsonify({"error": message}), status
    flash(message)
    return redirect(url_for("trash.trash_page"))


def _failure(exc: BaseException):
    if isinstance(exc, ValueError):
        return _api_error(str(exc) or "操作失败", 400)
    logger.exception("回收站操作失败")
    return _api_error("操作失败，请重试", 500)


# --- 行操作 -----------------------------------------------------------


@bp.post("/api/trash/restore")
def restore_rows_api():
    conn = get_conn()
    try:
        body = _payload()
        table = _require_rows_table(conn, body.get("table"))
        op = rows.restore_rows(conn, table, _ids(body.get("ids")), source="web")
        if op is None:
            conn.rollback()
            return _api_error("该行不在回收站", 409)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 统一映射
        conn.rollback()
        return _failure(exc)
    count = len(((audit.get_op(conn, op) or {}).get("payload") or {}).get("rows") or [])
    return _after_write(f"已恢复 {count} 行", {"op": op, "count": count})


@bp.post("/api/trash/purge")
def purge_rows_api():
    conn = get_conn()
    try:
        body = _payload()
        table = _require_rows_table(conn, body.get("table"))
        op = rows.purge_rows(conn, table, _ids(body.get("ids")), source="web")
        conn.commit()
    except Exception as exc:  # noqa: BLE001 统一映射
        conn.rollback()
        return _failure(exc)
    count = len(((audit.get_op(conn, op) or {}).get("payload") or {}).get("rows") or [])
    return _after_write(f"已彻底删除 {count} 行", {"op": op, "count": count})


# --- 表操作 -----------------------------------------------------------


@bp.post("/api/trash/tables/restore")
def restore_table_api():
    conn = get_conn()
    try:
        body = _payload()
        name = _require_table_name(body.get("name") or body.get("table"))
        op = schema.restore_table(conn, name, source="web")
        conn.commit()
    except Exception as exc:  # noqa: BLE001 统一映射
        conn.rollback()
        return _failure(exc)
    record = audit.get_op(conn, op) or {}
    restored = (record.get("payload") or {}).get("restored_as") or name
    return _after_write(
        f"已恢复表「{restored}」", {"op": op, "table": restored}
    )


@bp.post("/api/trash/tables/purge")
def purge_table_api():
    conn = get_conn()
    try:
        body = _payload()
        name = _require_table_name(body.get("name") or body.get("table"))
        # 同名多张已删除表时按名字只会命中最新一张；不可逆操作一律拒绝。
        duplicates = [
            item for item in schema.list_trashed_tables(conn) if item["name"] == name
        ]
        if len(duplicates) > 1:
            conn.rollback()
            return _api_error(
                "存在同名已删除表，请先恢复其中一张再处理", 409
            )
        op = schema.purge_table(conn, name, source="web")
        conn.commit()
    except Exception as exc:  # noqa: BLE001 统一映射
        conn.rollback()
        return _failure(exc)
    return _after_write(f"已彻底删除表「{name}」", {"op": op})
