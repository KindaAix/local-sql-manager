"""建表、表清单、字段元数据与自动索引。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Sequence

from . import audit
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
    """索引名带表名与列名长度前缀，避免 "a_b"+"c" 与 "a"+"b_c" 撞名。"""
    return f"__lsm_idx_{len(table)}_{table}_{len(column)}_{column}"


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
    _reject_reserved_name(name)
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

TRASH_PREFIX = "__lsm_trash_"


def _reject_reserved_name(name: str) -> None:
    """内部表与 sqlite 系统表的命名空间不允许建业务表。"""
    if str(name).lower().startswith(("__lsm_", "sqlite_")):
        raise ValueError(f"表名不能使用内部前缀：{name}")


def _ensure_user_table(conn: sqlite3.Connection, table: str) -> str:
    """校验是可操作的真实表，并返回库里登记的大小写形式。"""
    if str(table).lower().startswith(("__lsm_", "sqlite_")):
        raise ValueError(f"不能操作内部表：{table}")
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name = ? COLLATE NOCASE",
        (table,),
    ).fetchone()
    if row is None:
        raise ValueError(f"表不存在：{table}")
    return row[0]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """库里是否真的有这张表（大小写不敏感）。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table'"
        " AND name = ? COLLATE NOCASE",
        (name,),
    ).fetchone()
    return row is not None


def _name_taken(conn: sqlite3.Connection, name: str) -> bool:
    """表名是否已被表、视图或索引占用。

    SQLite 的对象名大小写不敏感且共享命名空间；改名的目标名必须按此判断，
    否则会漏过预检，最后抛 sqlite3.OperationalError 的英文原文。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    return row is not None


def _index_prefix(table: str) -> str:
    """该表自动索引名的公共前缀，与 _index_name 的格式保持一致。"""
    return f"__lsm_idx_{len(table)}_{table}_"


def _clear_metadata(conn: sqlite3.Connection, table: str) -> None:
    """删掉某个物理表名下残留的元数据行。

    __lsm_table 的登记是普通 INSERT，表被外部手段改动后可能留下同名孤儿行；
    重命名/恢复的目标名要先清干净，避免主键冲突。
    """
    for meta in ("__lsm_table", "__lsm_field", "__lsm_view"):
        conn.execute(f'DELETE FROM "{meta}" WHERE "table_name" = ?', (table,))


# 这些错误说明 RENAME 重解析 schema 时被失效的视图/触发器挡住，可以退回
# legacy 模式重试；其它 OperationalError 原样抛出。
_LEGACY_RENAME_HINTS = ("error in view", "error in trigger", "no such table")


def _sql_references_table(sql: str, table: str) -> bool:
    """SQL 中是否引用了 table（大小写不敏感，支持引号标识符）。

    只跳过字符串字面量与注释，其余按「出现即算引用」处理：列名与表名同名
    时会多报，但不会漏掉真正的 view/trigger 引用。
    """
    target = table.lower()
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "'":
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    break
                index += 1
            index += 1
            continue
        if sql[index : index + 2] == "--":
            newline = sql.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if sql[index : index + 2] == "/*":
            end = sql.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char in ('"', "\x60", "["):
            closer = "]" if char == "[" else char
            index += 1
            start = index
            parts: list[str] = []
            while index < length:
                if sql[index] == closer:
                    if (
                        closer != "]"
                        and index + 1 < length
                        and sql[index + 1] == closer
                    ):
                        parts.append(sql[start:index])
                        parts.append(closer)
                        index += 2
                        start = index
                        continue
                    break
                index += 1
            parts.append(sql[start:index])
            if "".join(parts).lower() == target:
                return True
            index += 1
            continue
        if char.isalnum() or char == "_" or ord(char) > 127:
            start = index
            while index < length and (
                sql[index].isalnum()
                or sql[index] == "_"
                or ord(sql[index]) > 127
            ):
                index += 1
            if sql[start:index].lower() == target:
                return True
            continue
        index += 1
    return False


def _referencing_objects(
    conn: sqlite3.Connection, table: str
) -> list[str]:
    """返回 SQL 里引用 table 的视图与「挂在其它表上」的触发器名。"""
    names: list[str] = []
    for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
        " WHERE sql IS NOT NULL AND type IN ('view', 'trigger')"
    ):
        kind, name, tbl_name, sql = row[0], row[1], row[2], row[3]
        if kind == "trigger" and str(tbl_name).lower() == table.lower():
            continue
        if _sql_references_table(sql, table):
            names.append(name)
    return names


def _rename_table_strict(conn: sqlite3.Connection, old: str, new: str) -> None:
    """优先用 modern 语义改表名（视图/触发器引用跟随新名），必要时回退 legacy。

    SQLite 3.25+ 的 RENAME 会重解析整个 schema：库里只要存在引用不存在对象
    的坏视图/触发器，连无关表的改名也会失败。此时若没有其它 view/触发器
    引用本表，可安全退回 legacy（只改 schema 文本、不重写引用）；若仍有引用，
    回退会让引用悬挂在旧名上，必须报错让用户先修复。
    连接上原本开启 legacy 时，首次尝试前先关掉，确保 modern 语义真正生效。
    """
    legacy = conn.execute("PRAGMA legacy_alter_table").fetchone()[0]
    if legacy:
        conn.execute("PRAGMA legacy_alter_table=OFF")
    try:
        try:
            conn.execute(f"ALTER TABLE {_quote(old)} RENAME TO {_quote(new)}")
            return
        except sqlite3.OperationalError as exc:
            if not any(hint in str(exc).lower() for hint in _LEGACY_RENAME_HINTS):
                raise
            references = _referencing_objects(conn, old)
            if references:
                raise ValueError(
                    "库中存在损坏的视图或触发器，且仍有对象引用该表"
                    f"（{'、'.join(references)}），请先修复后再操作"
                ) from exc
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute(f"ALTER TABLE {_quote(old)} RENAME TO {_quote(new)}")
    finally:
        conn.execute(f"PRAGMA legacy_alter_table={1 if legacy else 0}")


def _relocate_table(conn: sqlite3.Connection, old: str, new: str) -> None:
    """把表 old 改名为 new，字段/视图元数据与自动索引一并跟随。

    SQLite 改名不会改索引名，而索引名在库内必须唯一；不重建的话，回收站里
    的表会一直占着「__lsm_idx_<原名>_」这些名字，同名新表建索引时会被
    IF NOT EXISTS 静默跳过。
    """
    indexes = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
            (old,),
        )
        if row[0].startswith(_index_prefix(old))
    ]
    _rename_table_strict(conn, old, new)
    _clear_metadata(conn, new)
    for meta in ("__lsm_table", "__lsm_field", "__lsm_view"):
        conn.execute(
            f'UPDATE "{meta}" SET "table_name" = ? WHERE "table_name" = ?',
            (new, old),
        )
    for index in indexes:
        conn.execute(f"DROP INDEX IF EXISTS {_quote(index)}")
    ensure_indexes(conn, new)


def _trash_name(physical: str) -> str | None:
    """从 __lsm_trash_<op>_<原名> 还原原名；不是回收站名时返回 None。"""
    if not physical.startswith(TRASH_PREFIX):
        return None
    op, sep, name = physical[len(TRASH_PREFIX) :].partition("_")
    if not sep or not op.isdigit() or not name:
        return None
    return name


def _find_trashed(conn: sqlite3.Connection, name: str) -> list[tuple[int, str]]:
    """返回与原表名匹配的回收站表 [(trashed_op, 物理名)]，按 op 升序。"""
    rows = conn.execute(
        'SELECT "table_name", "trashed_op" FROM "__lsm_table"'
        ' WHERE "status" = ?',
        ("trashed",),
    ).fetchall()
    found = [(row[1] or 0, row[0]) for row in rows if _trash_name(row[0]) == name]
    return sorted(found)


def rename_table(
    conn: sqlite3.Connection, old: str, new: str, source: str = "web"
) -> int:
    """重命名表并记审计，返回 __lsm_op.id。

    调用方若已开启事务，本函数加入该事务；否则自身提交。
    """
    new = str(new).strip()
    if not new:
        raise ValueError("表名不能为空")
    _reject_reserved_name(new)
    old = _ensure_user_table(conn, old)
    if new == old:
        raise ValueError("新表名与原表名相同")
    if _name_taken(conn, new):
        raise ValueError(f"表已存在：{new}")
    with _savepoint(conn, "lsm_rename_table"):
        op = audit.record(
            conn, "rename_table", old, source, {"old": old, "new": new}
        )
        _relocate_table(conn, old, new)
    return op


def drop_table(conn: sqlite3.Connection, table: str, source: str = "web") -> int:
    """把表移入「已删除表」区：改名为 __lsm_trash_<op>_<name>，返回 op id。

    调用方若已开启事务，本函数加入该事务；否则自身提交。
    """
    table = _ensure_user_table(conn, table)
    with _savepoint(conn, "lsm_drop_table"):
        op = audit.record(conn, "drop_table", table, source, {"name": table})
        physical = f"{TRASH_PREFIX}{op}_{table}"
        _relocate_table(conn, table, physical)
        cur = conn.execute(
            'UPDATE "__lsm_table" SET "status" = ?, "trashed_op" = ?'
            ' WHERE "table_name" = ?',
            ("trashed", op, physical),
        )
        if cur.rowcount == 0:
            conn.execute(
                'INSERT INTO "__lsm_table"'
                ' ("table_name", "status", "created_at", "source_file", "trashed_op")'
                " VALUES (?, 'trashed', ?, NULL, ?)",
                (physical, utcnow_iso(), op),
            )
    return op


def list_trashed_tables(conn: sqlite3.Connection) -> list[dict]:
    """列出「已删除表」区：原表名、当前物理名与删除操作 id。"""
    rows = conn.execute(
        'SELECT "table_name", "trashed_op" FROM "__lsm_table"'
        ' WHERE "status" = ? ORDER BY "trashed_op", "table_name"',
        ("trashed",),
    ).fetchall()
    return [
        {
            "name": _trash_name(row[0]) or row[0],
            "table_name": row[0],
            "trashed_op": row[1],
        }
        for row in rows
    ]


def restore_table(conn: sqlite3.Connection, table: str, source: str = "web") -> int:
    """恢复回收站里的表；重名时自动加 _2、_3 后缀。返回 op id。

    调用方若已开启事务，本函数加入该事务；否则自身提交。
    """
    candidates = _find_trashed(conn, table)
    if not candidates:
        raise ValueError(f"回收站中没有该表：{table}")
    physical = candidates[-1][1]
    if not _table_exists(conn, physical):
        raise ValueError(f"回收站中的表已不存在：{physical}")
    target = table
    suffix = 2
    while _name_taken(conn, target):
        target = f"{table}_{suffix}"
        suffix += 1
    with _savepoint(conn, "lsm_restore_table"):
        op = audit.record(
            conn,
            "restore_table",
            table,
            source,
            {"name": table, "restored_as": target, "trashed_as": physical},
        )
        _relocate_table(conn, physical, target)
        conn.execute(
            'UPDATE "__lsm_table" SET "status" = ?, "trashed_op" = NULL'
            ' WHERE "table_name" = ?',
            ("active", target),
        )
    return op


def purge_table(conn: sqlite3.Connection, table: str, source: str = "web") -> int:
    """彻底删除回收站里的表并清理元数据；记 irreversible 审计，返回 op id。

    调用方若已开启事务，本函数加入该事务；否则自身提交。
    """
    name = _trash_name(table)
    if name is not None:
        physical = table
        if physical not in [p for _, p in _find_trashed(conn, name)]:
            raise ValueError(f"回收站中没有该表：{name}")
    else:
        candidates = _find_trashed(conn, table)
        if not candidates:
            raise ValueError(f"回收站中没有该表：{table}")
        name, physical = table, candidates[-1][1]
    if not _table_exists(conn, physical):
        raise ValueError(f"回收站中的表已不存在：{physical}")
    with _savepoint(conn, "lsm_purge_table"):
        op = audit.record(
            conn,
            "purge_table",
            name,
            source,
            {"name": name, "table_name": physical},
            irreversible=True,
        )
        conn.execute(f"DROP TABLE {_quote(physical)}")
        _clear_metadata(conn, physical)
    return op
