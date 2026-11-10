"""把视图或整表导出为 Excel / CSV。

只读操作：不写审计、不提交事务，也不依赖 Flask。Excel 用 write_only 工作簿
流式写入，逐行从游标取数，不把整表读进内存；先写同目录临时文件再原子改名，
失败不会留下半截文件，也不会破坏同名旧文件。

out_path 由调用方（路由 / MCP）负责归一化、扩展名白名单（.xlsx / .csv）与
导出目录限制；本模块不做沙箱，只按给定路径写。
"""

from __future__ import annotations

import base64
import csv
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import fields as dc_fields
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from .models import ViewConfig
from .query import build_order, build_where
from .schema import get_fields, list_tables

# 单次导出的行数上限；界面应先筛选再导出更大的表。
MAX_ROWS = 1_000_000

# 导出时附在业务列之后的辅助列（只读展示用）。
AUX_EXPORT_COLUMNS = ("created_at", "updated_at")

# 不随导出进入表格的内部列。
_INTERNAL_COLUMNS = frozenset(("id", "deleted_at"))

_ILLEGAL_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
_CSV_FORMULA_PREFIX = re.compile(r"^[=+\-@\t\r]")
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


def _require_table(conn: sqlite3.Connection, table: str) -> str:
    """校验是库内真实用户表，返回库里的实际表名；内部表直接拒绝。"""
    name = str(table)
    if name.lower().startswith(("__lsm_", "sqlite_")):
        raise ValueError(f"表不存在：{table}")
    names = list_tables(conn)
    if name in names:
        return name
    for existing in names:
        if existing.casefold() == name.casefold():
            return existing
    raise ValueError(f"表不存在：{table}")


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """表里实际存在的列名；调用前须已通过 _require_table 校验。"""
    cursor = conn.execute(f"SELECT * FROM {_quote(table)} LIMIT 0")
    return [item[0] for item in cursor.description]


def _require_fields(conn: sqlite3.Connection, table: str) -> list:
    """未启用编辑（没有字段元数据）的表不允许导出，避免静默少列。"""
    fields = get_fields(conn, table)
    if not fields:
        raise ValueError("该表尚未启用编辑，无法导出")
    return fields


def _export_columns(fields: list, config: ViewConfig, existing: list[str]) -> list[str]:
    """业务列（去掉隐藏列并按视图 order 重排）+ 辅助列，不含 id/deleted_at。"""
    present = set(existing)
    hidden = set(config.hidden or ())
    business = [
        field.name
        for field in fields
        if field.name in present and field.name not in hidden
    ]
    ordered = []
    for name in config.order or []:
        if name in business and name not in ordered:
            ordered.append(name)
    columns = ordered + [name for name in business if name not in ordered]
    columns += [
        name
        for name in AUX_EXPORT_COLUMNS
        if name in present and name not in columns
    ]
    return columns


def _where_clause(
    fields: list, config: ViewConfig, existing: list[str]
) -> tuple[str, list]:
    """软删过滤与视图筛选合并成不带 WHERE 的片段及参数。"""
    conditions = []
    params = []
    if "deleted_at" in existing:
        conditions.append('"deleted_at" IS NULL')
    where, where_params = build_where(config.filters, fields)
    if where:
        conditions.append(f"({where})")
        params.extend(where_params)
    return " AND ".join(conditions), params


def _has_rowid(conn: sqlite3.Connection, table: str) -> bool:
    try:
        conn.execute(f"SELECT rowid FROM {_quote(table)} LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return False
    return True


def _order_clause(
    conn: sqlite3.Connection,
    table: str,
    config: ViewConfig,
    existing: list[str],
) -> str:
    """视图排序/分组之外补一个稳定 tiebreaker，保证导出顺序确定。"""
    present = set(existing)
    sorts = [
        item
        for item in config.sorts or []
        if isinstance(item, dict) and item.get("column") in present
    ]
    group = config.group
    if not (isinstance(group, dict) and group.get("column") in present):
        group = None
    parts = []
    order = build_order(sorts, group)
    if order:
        parts.append(order)
    if "id" in present:
        parts.append('"id" ASC')
    elif _has_rowid(conn, table):
        parts.append("rowid ASC")
    return ", ".join(parts)


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


def _cell(worksheet, value):
    """Excel 单元格取值：bytes 转 base64；以 = 开头的文本强制按字符串写。"""
    if isinstance(value, bytes):
        value = base64.b64encode(value).decode("ascii")
    if isinstance(value, str) and value.startswith("="):
        cell = WriteOnlyCell(worksheet, value=value)
        cell.data_type = "s"
        return cell
    return value


def _csv_cell(value: object) -> str:
    """CSV 取值：None 写空串；字符串形似公式时前置单引号防注入。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, str) and _CSV_FORMULA_PREFIX.match(value):
        return "'" + value
    return str(value)


def _sheet_name(raw: object, used: set[str]) -> str:
    """去掉非法字符与首尾单引号、截断到 31 字符，重名时追加 -2/-3。"""
    base = _ILLEGAL_SHEET_CHARS.sub("", str(raw)).strip().strip("'").strip()
    base = (base or "Sheet")[:_MAX_SHEET_NAME]
    candidate = base
    index = 2
    while candidate.casefold() in used:
        suffix = f"-{index}"
        candidate = base[:_MAX_SHEET_NAME - len(suffix)] + suffix
        index += 1
    used.add(candidate.casefold())
    return candidate


@contextmanager
def _atomic_target(path: Path):
    """先写同目录临时文件，成功后再原子替换目标，失败清理临时文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=".lsm-export-", dir=str(path.parent)
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        yield temporary
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _prepare(
    conn: sqlite3.Connection, table: str, view: ViewConfig | dict | None
) -> tuple[str, list[str], str, list, str]:
    """校验表并返回实际表名、列、where 片段、参数与排序片段。"""
    config = _as_config(view)
    table = _require_table(conn, table)
    fields = _require_fields(conn, table)
    existing = _table_columns(conn, table)
    columns = _export_columns(fields, config, existing)
    if not columns:
        raise ValueError("没有可导出的列")
    where, params = _where_clause(fields, config, existing)
    return table, columns, where, params, _order_clause(conn, table, config, existing)


def export_view(
    conn: sqlite3.Connection,
    table: str,
    view: ViewConfig | dict | None,
    out_path: str | Path,
) -> Path:
    """按视图导出单表为 .xlsx，返回写入路径。"""
    table, columns, where, params, order = _prepare(conn, table, view)
    _check_row_limit(_count_rows(conn, table, where, params))

    path = Path(out_path)
    with _atomic_target(path) as temporary:
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet(title=_sheet_name(table, set()))
        sheet.append(columns)
        sql = _select_sql(table, columns, where, order)
        for row in conn.execute(sql, params):
            sheet.append([_cell(sheet, value) for value in row])
        workbook.save(temporary)
    return path


def export_table(
    conn: sqlite3.Connection, table: str, out_path: str | Path
) -> Path:
    """导出整表（默认视图：全部业务列、无筛选）。"""
    return export_view(conn, table, None, out_path)


def export_workbook(
    conn: sqlite3.Connection, tables: list[str], out_path: str | Path
) -> Path:
    """每张表一个 sheet 写入同一个 .xlsx，总行数共用 MAX_ROWS 上限。"""
    prepared = []
    total = 0
    for table in tables:
        name, columns, where, params, order = _prepare(conn, table, None)
        total += _count_rows(conn, name, where, params)
        prepared.append((name, columns, where, params, order))
    _check_row_limit(total)

    path = Path(out_path)
    with _atomic_target(path) as temporary:
        workbook = Workbook(write_only=True)
        used: set[str] = set()
        for name, columns, where, params, order in prepared:
            sheet = workbook.create_sheet(title=_sheet_name(name, used))
            sheet.append(columns)
            sql = _select_sql(name, columns, where, order)
            for row in conn.execute(sql, params):
                sheet.append([_cell(sheet, value) for value in row])
        workbook.save(temporary)
    return path


def export_csv(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str] | None,
    out_path: str | Path,
) -> Path:
    """导出单表为带 BOM 的 CSV；columns 为空时导出全部业务列。"""
    table = _require_table(conn, table)
    fields = _require_fields(conn, table)
    existing = _table_columns(conn, table)
    config = ViewConfig()

    if columns:
        selected = []
        for name in columns:
            if (
                name in existing
                and name not in _INTERNAL_COLUMNS
                and name not in selected
            ):
                selected.append(name)
    else:
        selected = _export_columns(fields, config, existing)
    if not selected:
        raise ValueError("没有可导出的列")

    where, params = _where_clause(fields, config, existing)
    _check_row_limit(_count_rows(conn, table, where, params))
    sql = _select_sql(
        table, selected, where, _order_clause(conn, table, config, existing)
    )

    path = Path(out_path)
    with _atomic_target(path) as temporary:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\r\n")
            writer.writerow(selected)
            for row in conn.execute(sql, params):
                writer.writerow([_csv_cell(value) for value in row])
    return path
