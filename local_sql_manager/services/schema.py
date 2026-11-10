"""建表、表清单、字段元数据与自动索引。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import Field
from .types import TYPES
from .util import utcnow_iso

# 受管表的附加列，见设计文档 §6。
AUX_COLUMNS = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("created_at", "TEXT NOT NULL"),
    ("updated_at", "TEXT NOT NULL"),
    ("deleted_at", "TEXT"),
)

# 逻辑类型到物理列类型的映射。
PHYSICAL_TYPES = {
    "text": "TEXT",
    "number": "REAL",
    "date": "TEXT",
    "checkbox": "INTEGER",
    "select": "TEXT",
    "multiselect": "TEXT",
    "url": "TEXT",
}

DEFAULT_FIELD_WIDTH = 140


@dataclass
class ColumnSpec:
    """要新建的一个业务列。"""

    name: str
    type: str
    options: tuple = ()


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _index_name(table: str, column: str) -> str:
    """索引名带表名长度前缀，避免 "a_b"+"c" 与 "a"+"b_c" 撞名。"""
    return f"__lsm_idx_{len(table)}_{table}_{column}"


@contextmanager
def _savepoint(conn: sqlite3.Connection, name: str):
    """在一个保存点内执行，失败回滚且不破坏调用方已开启的事务。"""
    conn.execute(f'SAVEPOINT "{name}"')
    try:
        yield
    except Exception:
        conn.execute(f'ROLLBACK TO "{name}"')
        conn.execute(f'RELEASE "{name}"')
        raise
    else:
        conn.execute(f'RELEASE "{name}"')


def create_table(
    conn: sqlite3.Connection,
    name: str,
    columns: Iterable[ColumnSpec],
    source_file: str | None = None,
) -> str:
    """建受管表：附加四列、登记元数据、为每个业务列建索引。

    调用方若已开启事务，本函数加入该事务；否则自身提交。
    """
    if str(name).startswith("__lsm_"):
        raise ValueError(f"表名不能使用内部前缀 __lsm_：{name}")
    specs = list(columns)
    for spec in specs:
        if spec.type not in TYPES:
            raise ValueError(f"未知字段类型：{spec.type}")

    definitions = [f"{_quote(col)} {decl}" for col, decl in AUX_COLUMNS]
    definitions.extend(f"{_quote(spec.name)} {PHYSICAL_TYPES[spec.type]}" for spec in specs)

    with _savepoint(conn, "lsm_create_table"):
        conn.execute(f"CREATE TABLE {_quote(name)} (\n  " + ",\n  ".join(definitions) + "\n)")
        conn.execute(
            """
            INSERT INTO "__lsm_table"
                ("table_name", "status", "created_at", "source_file", "trashed_op")
            VALUES (?, 'active', ?, ?, NULL)
            """,
            (name, utcnow_iso(), source_file),
        )
        for position, spec in enumerate(specs):
            options_json = (
                json.dumps(list(spec.options), ensure_ascii=False) if spec.options else None
            )
            conn.execute(
                """
                INSERT INTO "__lsm_field"
                    ("table_name", "column", "type", "options_json", "position", "width")
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, spec.name, spec.type, options_json, position, DEFAULT_FIELD_WIDTH),
            )
        ensure_indexes(conn, name)
    return name


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """列出用户可见的表，按创建顺序；内部表与 sqlite 系统表不出现。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid"
    ).fetchall()
    return [
        row[0]
        for row in rows
        if not row[0].startswith("__lsm_") and not row[0].startswith("sqlite_")
    ]


def get_fields(conn: sqlite3.Connection, table: str) -> list[Field]:
    """按 position 返回业务字段；未登记的表返回空列表。"""
    rows = conn.execute(
        """
        SELECT "column", "type", "options_json", "position", "width"
        FROM "__lsm_field"
        WHERE "table_name" = ?
        ORDER BY "position"
        """,
        (table,),
    ).fetchall()
    fields = []
    for row in rows:
        options = tuple(json.loads(row[2])) if row[2] else ()
        fields.append(Field(row[0], row[1], options, row[3], row[4]))
    return fields


def ensure_indexes(conn: sqlite3.Connection, table: str) -> None:
    """为每个业务列补建普通索引 __lsm_idx_<表名长度>_<表>_<列>，已存在则跳过。"""
    with _savepoint(conn, "lsm_ensure_indexes"):
        for field in get_fields(conn, table):
            index = _index_name(table, field.name)
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {_quote(index)} "
                f"ON {_quote(table)} ({_quote(field.name)})"
            )
