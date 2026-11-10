"""Excel / CSV 多 sheet 解析与导入。

parse_workbook 只读地读出可导入对象的预览（列名、行数、前 5 行样本、推断
类型、可疑列），CSV 视为只有一个 sheet（名字取文件 stem）；import_sheets
在调用方的事务里建表并分批写入，成功后记一条 kind="import" 的审计，不
自行提交。

类型推断沿用 services.types.infer_type，口径固定为：纯 YYYY-MM-DD 才算
date，带时分秒一律按 text；带前导零一律 text；单选（select）不自动判定。
推断只看每个 sheet 的前 INFER_SAMPLE_ROWS 行，导入本身仍处理全部行。
"""

from __future__ import annotations

import csv
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, Iterator, Sequence

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

# 类型推断的取样行数：超出的行照常导入，只是不参与推断。
INFER_SAMPLE_ROWS = 5000

# 表名与列名的长度上限，避免超长 sheet 名/表头生成难以使用的标识符。
MAX_NAME_LENGTH = 64

# 受管表自带或 SQLite 内建的列名，业务列必须避开（大小写不敏感）。
_RESERVED_COLUMNS = frozenset(
    {"id", "created_at", "updated_at", "deleted_at", "rowid"}
)

# 导入与预览都来自界面，MCP 若需区分可后续加 source 参数。
_SOURCE = "web"

# Excel 工作表名本身不允许这些字符，这里仍做兜底清洗。
_FORBIDDEN_CHARS = set('[]:*?/\\')

_RESERVED_PREFIXES = ("__lsm_", "sqlite_")

# 中文 Windows 上 Excel 另存的 CSV 常见 ANSI/GBK，按此顺序尝试解码。
_CSV_ENCODINGS = ("utf-8-sig", "gb18030")

_CSV_ENCODING_ERROR = "无法读取文件：请另存为 UTF-8 或 GBK 编码的 CSV"


@dataclass
class SheetPreview:
    """一个可导入 sheet 的预览。sampled 表示类型只按前 5000 行推断。"""

    name: str
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    sample: list[list[str]] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)
    suspicious: list[str] = field(default_factory=list)
    empty: bool = True
    sampled: bool = False


@dataclass
class _Sheet:
    """统一的 sheet 数据源；rows 每次调用都从头迭代。"""

    name: str
    columns: list[str]
    rows: Callable[[], Iterator[list[str]]]


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


def _unique_name(base: str, used: set[str]) -> str:
    """在 used（小写）里取一个不重复的名字，整体不超过 MAX_NAME_LENGTH。"""
    name = base
    suffix = 2
    while name.lower() in used:
        tail = f"_{suffix}"
        name = base[: MAX_NAME_LENGTH - len(tail)] + tail
        suffix += 1
    used.add(name.lower())
    return name


def _header_names(raw: Sequence[object]) -> list[str]:
    """首行转列名：去尾部空列、空列名补 ColumnN、重名与内部列名消歧。"""
    texts = [_cell_text(value) for value in raw]
    while texts and not texts[-1]:
        texts.pop()

    # 受管表会无条件加 id/created_at/updated_at/deleted_at，表头撞上这些
    # 名字时必须先改名，否则 CREATE TABLE 会因重复列名失败。
    used: set[str] = set(_RESERVED_COLUMNS)
    names: list[str] = []
    for index, text in enumerate(texts, start=1):
        base = (text or f"Column{index}")[:MAX_NAME_LENGTH]
        names.append(_unique_name(base, used))
    return names


def _row_values(raw: Sequence[object], width: int) -> list[str]:
    texts = [_cell_text(value) for value in list(raw)[:width]]
    if len(texts) < width:
        texts.extend([""] * (width - len(texts)))
    return texts


def _worksheet_sheet(worksheet) -> _Sheet:
    """把 openpyxl 工作表包成可反复迭代的 _Sheet。"""
    columns: list[str] = []
    for raw in worksheet.iter_rows(values_only=True):
        if _is_blank_row(raw):
            continue
        columns = _header_names(raw)
        break

    def rows() -> Iterator[list[str]]:
        if not columns:
            return
        width = len(columns)
        header_seen = False
        for raw in worksheet.iter_rows(values_only=True):
            if _is_blank_row(raw):
                continue
            if not header_seen:
                header_seen = True
                continue
            yield _row_values(raw, width)

    return _Sheet(worksheet.title, columns, rows)


def _is_csv(path) -> bool:
    return str(path).lower().endswith(".csv")


def _csv_encoding(path) -> str:
    """优先 utf-8-sig，失败回退 gb18030；都失败时给中文提示。"""
    error: UnicodeDecodeError | None = None
    for encoding in _CSV_ENCODINGS:
        try:
            with open(path, "r", encoding=encoding, newline="") as handle:
                for _line in handle:
                    pass
            return encoding
        except UnicodeDecodeError as exc:
            error = exc
        except OSError as exc:
            raise ValueError(f"无法读取文件：{exc}") from exc
    raise ValueError(_CSV_ENCODING_ERROR) from error


def _csv_lines(path, encoding: str) -> Iterator[list[str]]:
    """逐行读 CSV；csv 模块的底层错误统一转成中文 ValueError。"""
    try:
        handle = open(path, "r", encoding=encoding, newline="")
    except OSError as exc:
        raise ValueError(f"无法读取文件：{exc}") from exc
    with handle:
        try:
            for raw in csv.reader(handle):
                # Python 3.11+ 的 csv 不再对 NUL 报错，这里主动拒绝，
                # 避免不可见控制字符一路写进 SQLite。
                if any("\x00" in cell for cell in raw):
                    raise ValueError("无法读取文件：CSV 内容不合法（含 NUL 字符）")
                yield raw
        except csv.Error as exc:
            raise ValueError(f"无法读取文件：CSV 内容不合法（{exc}）") from exc


def _read_csv_header(path, encoding: str) -> list[str]:
    for raw in _csv_lines(path, encoding):
        if _is_blank_row(raw):
            continue
        return _header_names(raw)
    return []


def _csv_sheet(path) -> _Sheet:
    """CSV 只有一个 sheet，名字取文件 stem；utf-8-sig / gb18030 兼容两种常见编码。"""
    encoding = _csv_encoding(path)
    columns = _read_csv_header(path, encoding)

    def rows() -> Iterator[list[str]]:
        if not columns:
            return
        width = len(columns)
        header_seen = False
        for raw in _csv_lines(path, encoding):
            if _is_blank_row(raw):
                continue
            if not header_seen:
                header_seen = True
                continue
            yield _row_values(raw, width)

    return _Sheet(Path(path).stem, columns, rows)


def _open_workbook(path):
    if str(path).lower().endswith(".xls"):
        raise ValueError("暂不支持 .xls 旧格式，请另存为 .xlsx 后再导入")
    try:
        return load_workbook(filename=str(path), read_only=True, data_only=True)
    except (InvalidFileException, OSError) as exc:
        raise ValueError(f"无法读取文件：{exc}") from exc


def _preview(sheet: _Sheet) -> SheetPreview:
    if not sheet.columns:
        return SheetPreview(name=sheet.name)

    values: list[list[str]] = [[] for _ in sheet.columns]
    sample: list[list[str]] = []
    row_count = 0
    for row in sheet.rows():
        row_count += 1
        if len(sample) < SAMPLE_ROWS:
            sample.append(row)
        if row_count <= INFER_SAMPLE_ROWS:
            for index, text in enumerate(row):
                if text:
                    values[index].append(text)

    types: dict[str, str] = {}
    suspicious: list[str] = []
    for column, column_values in zip(sheet.columns, values):
        type_, odd = infer_type(column_values)
        types[column] = type_
        if odd:
            suspicious.append(column)

    return SheetPreview(
        name=sheet.name,
        columns=list(sheet.columns),
        row_count=row_count,
        sample=sample,
        types=types,
        suspicious=suspicious,
        # 只有表头没有数据也按空表处理，界面才会灰掉、不会建 0 行表。
        empty=row_count == 0,
        sampled=row_count > INFER_SAMPLE_ROWS,
    )


def parse_workbook(path) -> list[SheetPreview]:
    """解析 xlsx 的每个 sheet，或单个 CSV；顺序与文件一致。"""
    if _is_csv(path):
        return [_preview(_csv_sheet(path))]

    workbook = _open_workbook(path)
    try:
        return [
            _preview(_worksheet_sheet(worksheet)) for worksheet in workbook.worksheets
        ]
    finally:
        workbook.close()


def _sanitize_table_name(name: object) -> str:
    """把 sheet 名清成可用的表名：替换非法字符、避开内部命名空间、截断。"""
    text = "".join(
        "_" if char in _FORBIDDEN_CHARS or ord(char) < 32 else char
        for char in str(name)
    ).strip()
    if not text:
        text = "Sheet1"
    if text.lower().startswith(_RESERVED_PREFIXES):
        text = "_" + text
    return text[:MAX_NAME_LENGTH]


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
        tail = f"_{suffix}"
        candidate = base[: MAX_NAME_LENGTH - len(tail)] + tail
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


def _resolve_types(sheet: _Sheet, overrides: dict) -> dict[str, str]:
    """合并手动指定的类型与推断类型；未指定的列推断前 5000 行。"""
    given = overrides.get(sheet.name) or {}
    for column, type_ in given.items():
        if column not in sheet.columns:
            raise ValueError(f"工作表 {sheet.name} 没有列：{column}")
        if type_ not in TYPES:
            raise ValueError(f"未知字段类型：{type_}")

    types = {column: given[column] for column in sheet.columns if column in given}
    missing = [column for column in sheet.columns if column not in given]
    if not missing:
        return types

    values: dict[str, list[str]] = {column: [] for column in missing}
    indexes = {
        column: index
        for index, column in enumerate(sheet.columns)
        if column in values
    }
    for position, row in enumerate(sheet.rows()):
        if position >= INFER_SAMPLE_ROWS:
            break
        for column, index in indexes.items():
            if row[index]:
                values[column].append(row[index])
    for column in missing:
        types[column] = infer_type(values[column])[0]
    return types


def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    sheet_name: str,
    columns: list[str],
    column_types: dict[str, str],
    rows: Iterator[list[str]],
) -> int:
    """分批写入数据行，返回写入了多少行。

    报错行号按「非空数据行」计数，表头算第 1 行；用于把脏值定位到
    sheet / 行 / 列，而不是抛出 sqlite/py 的英文原文。
    """
    now = utcnow_iso()
    names = ["created_at", "updated_at"] + columns
    sql = (
        f"INSERT INTO {_quote(table)} "
        f"({', '.join(_quote(name) for name in names)}) "
        f"VALUES ({', '.join('?' for _ in names)})"
    )

    batch: list[list[object]] = []
    row_count = 0
    for row_no, row in enumerate(rows, start=2):
        values = []
        for text, column in zip(row, columns):
            type_ = column_types[column]
            try:
                values.append(coerce(text, type_))
            except ValueError as exc:
                raise ValueError(
                    f"工作表 {sheet_name} 第 {row_no} 行「{column}」"
                    f"无法转换为 {type_}：{text!r}"
                ) from exc
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

    支持 .xlsx（多 sheet）与 .csv（单 sheet，名字取文件 stem）。on_conflict：
    overwrite 把同名旧表移入回收站后重建；copy 新建带后缀的副本；skip 跳过
    同名表。空 sheet 直接跳过。整批一个事务，任一步失败全部回滚；成功后记
    一条 kind="import" 的审计。
    """
    if on_conflict not in ("overwrite", "copy", "skip"):
        raise ValueError(f"未知的同名处理方式：{on_conflict}")
    selected = list(dict.fromkeys(str(name) for name in selected or []))
    if not selected:
        return []

    overrides = types or {}
    if not isinstance(overrides, dict):
        raise ValueError("类型覆盖必须是 {sheet: {column: type}} 形式")

    # 同一次导入里两张 sheet 清洗/截断后可能同名，必须提前拒绝：否则 skip
    # 会静默少导一张，overwrite 会把本次刚建的表当成旧表移进回收站。
    planned: dict[str, str] = {}
    for sheet_name in selected:
        table_name = _sanitize_table_name(sheet_name)
        key = table_name.lower()
        if key in planned:
            raise ValueError(f"多个工作表清洗后同名：{table_name}")
        planned[key] = sheet_name

    created: list[str] = []
    details: list[dict] = []
    filename = os.path.basename(str(path))

    workbook = None
    try:
        if _is_csv(path):
            sheets = {sheet.name: sheet for sheet in [_csv_sheet(path)]}
        else:
            workbook = _open_workbook(path)
            sheets = {
                worksheet.title: _worksheet_sheet(worksheet)
                for worksheet in workbook.worksheets
            }

        with _import_txn(conn):
            for sheet_name in selected:
                sheet = sheets.get(sheet_name)
                if sheet is None:
                    raise ValueError(f"文件里没有工作表：{sheet_name}")
                if not sheet.columns:
                    continue
                column_types = _resolve_types(sheet, overrides)
                table, overwritten = _plan_table(conn, sheet_name, on_conflict)
                if table is None:
                    continue
                create_table(
                    conn,
                    table,
                    [
                        ColumnSpec(column, column_types[column])
                        for column in sheet.columns
                    ],
                    source_file=filename,
                )
                row_count = _insert_rows(
                    conn,
                    table,
                    sheet_name,
                    list(sheet.columns),
                    column_types,
                    sheet.rows(),
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
        if workbook is not None:
            workbook.close()
    return created