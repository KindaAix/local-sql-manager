"""把视图或整表导出为 Excel / CSV。

只读操作：不写审计、不提交事务，也不依赖 Flask。Excel 用 write_only 工作簿
流式写入，逐行从游标取数，不把整表读进内存。
"""

from __future__ import annotations

import base64
import csv
import re
import sqlite3
from dataclasses import fields as dc_fields
from pathlib import Path

from openpyxl import Workbook

from .models import ViewConfig
from .query import build_order, build_where
from .schema import get_fields

# 单次导出的行数上限；界面应先筛选再导出更大的表。
MAX_ROWS = 1_000_000

# 导出时附在业务列之后的辅助列（只读展示用）。
AUX_EXPORT_COLUMNS = ("created_at", "updated_at")

_ILLEGAL_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
_MAX_SHEET_NAME = 31
_CONFIG_FIELDS = {item.name for item in dc_fields(ViewConfig)}


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _as_config(view: ViewConfig | dict | None) -> ViewConfig:
    """允许路由层传 JSON 反序列化出的 dict 或 None（默认视图）。"""
    if view is None:
        return ViewConfig()
    if isinstance(view, ViewConfig):
        return view
    if isinstance(view, dict):
        return ViewConfig(**{k: v for k, v in view.items() if k in _CONFIG_FIELDS})
    raise ValueError("视图配置类型不正确")


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """表里实际存在的列名；表不存在时抛中文 ValueError。"""
    try:
        cursor = conn.execute(f"SELECT * FROM {_quote(table)} LIMIT 0")
    except sqlite3.Error:
        raise ValueError(f"表不存在：{table}") from None
    return [item[0] for item in cursor.description]


def _export_columns(
    conn: sqlite3.Connection, table: str, config: ViewConfig, existing: list[str]
) -> list[str]:
    """业务列（去掉隐藏列并按视图 order 重排）+ 辅助列，不含 id/deleted_at。"""
    present = set(existing)
    business = [
        field.name
        for field in get_fields(conn, table)
        if field.name in present and field.name not in config.hidden
    ]
    ordered = [name for name in config.order or [] if name in business]
    columns = ordered + [name for name in business if name not in ordered]
    columns += [name for name in AUX_EXPORT_COLUMNS if name in present]
    return columns


def _where_clause(
    conn: sqlite3.Connection,
    table: str,
    config: ViewConfig,
    existing: list[str],
) -> tuple[str, list]:
    """软删过滤与视图筛选合并成不带 WHERE 的片段及参数。"""
    conditions = []
    params = []
    if "deleted_at" in existing:
        conditions.append('"deleted_at" IS NULL')
    where, where_params = build_where(config.filters, get_fields(conn, table))
    if where:
        conditions.append(f"({where})")
        params.extend(where_params)
    return " AND ".join(conditions), params


def _order_clause(
    config: ViewConfig, existing: list[str]
) -> str:
    """只保留真实存在的列，避免视图配置里的悬空引用导致 SQL 报错。"""
    present = set(existing)
    sorts = [
        item
        for item in config.sorts or []
        if isinstance(item, dict) and item.get("column") in present
    ]
    group = config.group
    if not (
        isinstance(group, dict) and group.get("column") in present
    ):
        group = None
    return build_order(sorts, group)


def _select_sql(table: str, columns: list[str], where: str, order: str) -> str:
    sql = f"SELECT {', '.join(_quote(name) for name in columns)} FROM {_quote(table)}"
    if where:
        sql += " WHERE " + where
    if order:
        sql += " ORDER BY " + order
    return sql


def _count_rows(conn: sqlite3.Connection, table: str, where: str, params: list) -> int:
    sql = f"SELECT COUNT(*) FROM {_quote(table)}"
    if where:
        sql += " WHERE " + where
    return conn.execute(sql, params).fetchone()[0]


def _check_row_limit(total: int) -> None:
    if total > MAX_ROWS:
        raise ValueError(f"导出行数超过上限 {MAX_ROWS} 行，请先筛选")


def _cell(value: object):
    """Excel 单元格取值：bytes 转 base64 文本，其余按原值写。"""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value)


def _sheet_name(raw: object, used: set[str]) -> str:
    """去掉 Excel 非法字符、截断到 31 字符，重名时追加 -2/-3。"""
    base = _ILLEGAL_SHEET_CHARS.sub("", str(raw)).strip() or "Sheet"
    base = base[:_MAX_SHEET_NAME]
    candidate = base
    index = 2
    while candidate.casefold() in used:
        suffix = f"-{index}"
        candidate = base[:_MAX_SHEET_NAME - len(suffix)] + suffix
        index += 1
    used.add(candidate.casefold())
    return candidate


def _prepare_path(out_path: str | Path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _prepare(
    conn: sqlite3.Connection, table: str, view: ViewConfig | dict | None
) -> tuple[list[str], str, list, str]:
    """返回列、where 片段、参数与排序片段。"""
    config = _as_config(view)
    existing = _table_columns(conn, table)
    columns = _export_columns(conn, table, config, existing)
    where, params = _where_clause(conn, table, config, existing)
    return columns, where, params, _order_clause(config, existing)


def export_view(
    conn: sqlite3.Connection,
    table: str,
    view: ViewConfig | dict | None,
    out_path: str | Path,
) -> Path:
    """按视图导出单表为 .xlsx，返回写入路径。"""
    columns, where, params, order = _prepare(conn, table, view)
    _check_row_limit(_count_rows(conn, table, where, params))

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title=_sheet_name(table, set()))
    sheet.append(columns)
    sql = _select_sql(table, columns, where, order)
    for row in conn.execute(sql, params):
        sheet.append([_cell(value) for value in row])

    path = _prepare_path(out_path)
    workbook.save(path)
    return path


def export_table(
    conn: sqlite3.Connection, table: str, out_path: str | Path
) -> Path:
    """导出整表（默认视图：全部业务列、无筛选）。"""
    return export_view(conn, table, None, out_path)


def export_workbook(
    conn: sqlite3.Connection, tables: list[str], out_path: str | Path
) -> Path:
    """每张表一个 sheet 写入同一个 .xlsx。"""
    workbook = Workbook(write_only=True)
    used: set[str] = set()
    for table in tables:
        columns, where, params, order = _prepare(conn, table, None)
        _check_row_limit(_count_rows(conn, table, where, params))
        sheet = workbook.create_sheet(title=_sheet_name(table, used))
        sheet.append(columns)
        sql = _select_sql(table, columns, where, order)
        for row in conn.execute(sql, params):
            sheet.append([_cell(value) for value in row])

    path = _prepare_path(out_path)
    workbook.save(path)
    return path


def export_csv(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str] | None,
    out_path: str | Path,
) -> Path:
    """导出单表为带 BOM 的 CSV，Excel 打开不乱码。"""
    config = ViewConfig()
    existing = _table_columns(conn, table)
    if columns:
        selected = [name for name in columns if name in existing]
    else:
        selected = _export_columns(conn, table, config, existing)

    where, params = _where_clause(conn, table, config, existing)
    _check_row_limit(_count_rows(conn, table, where, params))

    path = _prepare_path(out_path)
    sql = _select_sql(table, selected, where, _order_clause(config, existing))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(selected)
        for row in conn.execute(sql, params):
            writer.writerow([_csv_cell(value) for value in row])
    return path
