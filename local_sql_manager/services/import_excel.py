"""Excel 多 sheet 解析与导入。

parse_workbook 只读地读出每个 sheet 的预览（列名、行数、前 5 行样本、推断
类型、可疑列），供导入界面勾选；import_sheets 在调用方的事务里建表并分批
写入，成功后记一条 kind="import" 的审计，不自行提交。

类型推断沿用 services.types.infer_type，口径固定为：纯 YYYY-MM-DD 才算
date，带时分秒一律按 text；带前导零一律 text；单选（select）不自动判定。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Iterator, Sequence

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from . import audit
from .schema import TRASH_PREFIX, ColumnSpec, create_table, drop_table
from .types import TYPES, coerce, infer_type
from .util import utcnow_iso

# 每批 executemany 的行数；批数不影响事务边界。
BATCH_SIZE = 500

# 预览里展示的样本行数。
SAMPLE_ROWS = 5

# 导入与预览都来自界面，MCP 若需区分可后续加 source 参数。
_SOURCE = "web"

# Excel 工作表名本身不允许这些字符，这里仍做兜底清洗。
_FORBIDDEN_CHARS = set('[]:*?/\\')

_RESERVED_PREFIXES = ("__lsm_", "sqlite_")


@dataclass
class SheetPreview:
    """一个 sheet 的导入预览。"""

    name: str
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    sample: list[list[str]] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)
    suspicious: list[str] = field(default_factory=list)
    empty: bool = True


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _cell_text(value: object) -> str:
    """把单元格值转成用于预览与推断的文本。

    整点 datetime 收敛为 YYYY-MM-DD，这样 Excel 的日期单元格仍是 date 类型；
    带时分秒的保留时间部分，按约定推断为 text。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.time() == time(0, 0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _is_blank_row(values: Sequence[object]) -> bool:
    return all(not _cell_text(value) for value in values)


def _header_names(raw: Sequence[object]) -> list[str]:
    """首行转列名：去尾部空列、空列名补 ColumnN、重名加 _2 后缀。"""
    texts = [_cell_text(value) for value in raw]
    while texts and not texts[-1]:
        texts.pop()

    names: list[str] = []
    # SQLite 的列名大小写不敏感，"Name" 与 "name" 也算重名。
    used: set[str] = set()
    for index, text in enumerate(texts, start=1):
        base = text or f"Column{index}"
        name = base
        suffix = 2
        while name.lower() in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name.lower())
        names.append(name)
    return names


def _row_values(raw: Sequence[object], width: int) -> list[str]:
    texts = [_cell_text(value) for value in list(raw)[:width]]
    if len(texts) < width:
        texts.extend([""] * (width - len(texts)))
    return texts


def _open_sheet(worksheet) -> tuple[list[str], Iterator[list[str]]]:
    """返回 (消歧后的列名, 数据行迭代器)；数据行已转成文本并跳过空行。"""
    iterator = worksheet.iter_rows(values_only=True)
    for raw in iterator:
        if _is_blank_row(raw):
            continue
        columns = _header_names(raw)
        break
    else:
        return [], iter(())

    width = len(columns)

    def rows() -> Iterator[list[str]]:
        for raw in iterator:
            if _is_blank_row(raw):
                continue
            yield _row_values(raw, width)

    return columns, rows()


def _open_workbook(path):
    if str(path).lower().endswith(".xls"):
        raise ValueError("暂不支持 .xls 旧格式，请另存为 .xlsx 后再导入")
    try:
        return load_workbook(filename=str(path), read_only=True, data_only=True)
    except (InvalidFileException, OSError) as exc:
        raise ValueError(f"无法读取工作簿：{exc}") from exc


def _preview_sheet(worksheet) -> SheetPreview:
    columns, rows = _open_sheet(worksheet)
    if not columns:
        return SheetPreview(name=worksheet.title)

    values: list[list[str]] = [[] for _ in columns]
    sample: list[list[str]] = []
    row_count = 0
    for row in rows:
        row_count += 1
        if len(sample) < SAMPLE_ROWS:
            sample.append(row)
        for index, text in enumerate(row):
            if text:
                values[index].append(text)

    types: dict[str, str] = {}
    suspicious: list[str] = []
    for column, column_values in zip(columns, values):
        type_, odd = infer_type(column_values)
        types[column] = type_
        if odd:
            suspicious.append(column)

    return SheetPreview(
        name=worksheet.title,
        columns=columns,
        row_count=row_count,
        sample=sample,
        types=types,
        suspicious=suspicious,
        empty=False,
    )


def parse_workbook(path) -> list[SheetPreview]:
    """解析工作簿的每个 sheet，顺序与工作簿一致。"""
    workbook = _open_workbook(path)
    try:
        return [_preview_sheet(worksheet) for worksheet in workbook.worksheets]
    finally:
        workbook.close()


def _sanitize_table_name(name: object) -> str:
    """把 sheet 名清成可用的表名：替换非法字符、避开内部命名空间。"""
    text = "".join(
        "_" if char in _FORBIDDEN_CHARS or ord(char) < 32 else char
        for char in str(name)
    ).strip()
    if not text:
        return "Sheet1"
    if text.lower().startswith(_RESERVED_PREFIXES):
        return "_" + text
    return text


def _find_object(conn: sqlite3.Connection, name: str) -> tuple[str, str] | None:
    """按 SQLite 的大小写不敏感规则找同名对象，返回 (名称, 类型)。"""
    row = conn.execute(
        "SELECT name, type FROM sqlite_master WHERE name = ? COLLATE NOCASE",
        (name,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def _copy_name(conn: sqlite3.Connection, base: str) -> str:
    suffix = 2
    while True:
        candidate = f"{base}_{suffix}"
        if _find_object(conn, candidate) is None:
            return candidate
        suffix += 1


def _plan_table(
    conn: sqlite3.Connection, sheet_name: str, on_conflict: str
) -> tuple[str | None, dict | None]:
    """决定这张 sheet 要建的表名，以及是否把旧表移入回收站。

    返回 (新表名, 被覆盖旧表的信息)；跳过时新表名为 None。旧表信息里的
    trashed_as 是回收站物理名，供撤销导入时原样恢复。
    """
    base = _sanitize_table_name(sheet_name)
    existing = _find_object(conn, base)
    if existing is None:
        return base, None
    existing_name, existing_type = existing
    if on_conflict == "skip":
        return None, None
    if on_conflict == "copy":
        return _copy_name(conn, base), None
    if existing_type != "table":
        raise ValueError(f"同名对象已存在，无法覆盖：{existing_name}")
    op = drop_table(conn, existing_name, _SOURCE)
    return base, {
        "name": existing_name,
        "op": op,
        "trashed_as": f"{TRASH_PREFIX}{op}_{existing_name}",
    }


def _resolve_types(
    sheet_name: str,
    columns: list[str],
    worksheet,
    overrides: dict,
) -> dict[str, str]:
    """合并手动指定的类型与推断类型；未指定的列用整列数据推断。"""
    given = overrides.get(sheet_name) or {}
    for column, type_ in given.items():
        if column not in columns:
            raise ValueError(f"工作表 {sheet_name} 没有列：{column}")
        if type_ not in TYPES:
            raise ValueError(f"未知字段类型：{type_}")

    types = {column: given[column] for column in columns if column in given}
    missing = [column for column in columns if column not in given]
    if not missing:
        return types

    values: dict[str, list[str]] = {column: [] for column in missing}
    _, rows = _open_sheet(worksheet)
    indexes = {column: index for index, column in enumerate(columns) if column in values}
    for row in rows:
        for column, index in indexes.items():
            if row[index]:
                values[column].append(row[index])
    for column in missing:
        types[column] = infer_type(values[column])[0]
    return types


def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    column_types: dict[str, str],
    rows: Iterator[list[str]],
) -> int:
    """分批写入数据行，返回写入了多少行。"""
    now = utcnow_iso()
    names = ["created_at", "updated_at"] + columns
    sql = (
        f"INSERT INTO {_quote(table)} "
        f"({', '.join(_quote(name) for name in names)}) "
        f"VALUES ({', '.join('?' for _ in names)})"
    )

    batch: list[list[object]] = []
    row_count = 0
    for row in rows:
        values = [
            coerce(text, column_types[column])
            for text, column in zip(row, columns)
        ]
        batch.append([now, now] + values)
        row_count += 1
        if len(batch) >= BATCH_SIZE:
            conn.executemany(sql, batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
    return row_count


@contextmanager
def _import_txn(conn: sqlite3.Connection):
    """整批导入一个事务：成功不提交，失败整体回滚。

    与 rows._write_txn 一致，调用方负责最终 commit；包一层保存点是为了
    在调用方已有事务时不破坏它之前的写入。
    """
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    conn.execute('SAVEPOINT "lsm_import"')
    try:
        yield
    except BaseException:
        conn.execute('ROLLBACK TO "lsm_import"')
        conn.execute('RELEASE "lsm_import"')
        if started:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute('RELEASE "lsm_import"')


def import_sheets(
    conn: sqlite3.Connection,
    path,
    selected: Sequence[str],
    types: dict[str, dict[str, str]] | None = None,
    on_conflict: str = "skip",
) -> list[str]:
    """把选中的 sheet 各建成一张新表，返回新建的表名（按处理顺序）。

    on_conflict：overwrite 把同名旧表移入回收站后重建；copy 新建带后缀的
    副本；skip 跳过同名表。空 sheet 直接跳过。整批一个事务，任一步失败
    全部回滚；成功后记一条 kind="import" 的审计。
    """
    if on_conflict not in ("overwrite", "copy", "skip"):
        raise ValueError(f"未知的同名处理方式：{on_conflict}")
    selected = [str(name) for name in selected or []]
    if not selected:
        return []

    overrides = types or {}
    if not isinstance(overrides, dict):
        raise ValueError("类型覆盖必须是 {sheet: {column: type}} 形式")

    created: list[str] = []
    details: list[dict] = []
    filename = os.path.basename(str(path))
    workbook = _open_workbook(path)
    try:
        sheets = {worksheet.title: worksheet for worksheet in workbook.worksheets}
        with _import_txn(conn):
            for sheet_name in selected:
                worksheet = sheets.get(sheet_name)
                if worksheet is None:
                    raise ValueError(f"工作簿中没有工作表：{sheet_name}")
                columns, rows = _open_sheet(worksheet)
                if not columns:
                    continue
                column_types = _resolve_types(
                    sheet_name, columns, worksheet, overrides
                )
                table, overwritten = _plan_table(conn, sheet_name, on_conflict)
                if table is None:
                    continue
                create_table(
                    conn,
                    table,
                    [ColumnSpec(column, column_types[column]) for column in columns],
                    source_file=filename,
                )
                row_count = _insert_rows(
                    conn, table, columns, column_types, rows
                )
                created.append(table)
                details.append(
                    {
                        "sheet": sheet_name,
                        "table": table,
                        "rows": row_count,
                        "overwritten": (
                            overwritten["name"] if overwritten else None
                        ),
                        "overwritten_op": overwritten["op"] if overwritten else None,
                        "overwritten_trashed_as": (
                            overwritten["trashed_as"] if overwritten else None
                        ),
                    }
                )

            if created:
                # 行数要等写入完成才知道，因此审计在数据之后写；仍在同一事务里，
                # 失败时一起回滚，不会留下指向空表的 import 记录。
                audit.record(
                    conn,
                    "import",
                    ",".join(created),
                    _SOURCE,
                    {"file": filename, "tables": list(created), "sheets": details},
                )
    finally:
        workbook.close()
    return created