"""多表合并：把多张受管表的行汇入一张新表。

语义
====

- 合并只新建目标表，源表的数据与元数据都不改动。
- 只复制源表中未软删（deleted_at IS NULL）的行；软删行留在源表里，
  不会被「复活」到目标表。
- 目标表是普通受管表：由 schema.create_table 建表，因此自动带
  id / created_at / updated_at / deleted_at 四列与业务列索引；
  行 id 重新分配，created_at / updated_at 取合并时刻。

列对齐
======

列名先做 strip().lower() 归一化再比较，目标列的显示名取首次出现的原始
写法（去掉首尾空格）。没有给 mapping 时，目标列是各源表业务列的并集，
顺序为按 tables 顺序扫描时各列首次出现的顺序；某张源表没有该列时补
NULL（不做类型强转）。

mapping 的口径是「目标列 -> {源表: 源列}」::

    mapping = {"金额": {"一月": "金额", "二月": "Amount"}}

键是要出现在目标表里的列名，顺序即目标列顺序；值指明每张源表用哪一列
填充它。给了 mapping 时只有映射里列出的目标列会进入新表，没有列出的源表
该列填 NULL。plan_merge 与 merge_tables 共用同一套解析，所以确认页看到的
列就是最终建出的列。

目标列的逻辑类型取 tables 顺序中第一张包含该列的源表的字段类型；若同一列
在源表间类型不一致，记入 conflicts（列名 + 各表类型），但不阻止合并——
物理列按首个类型建，值依赖 SQLite 列亲和性写入，不做显式转换。

审计与撤销
==========

merge_tables 写一条 kind="merge" 的审计，payload 含 target / tables /
columns / conflicts / rows，且不提交事务：调用方把合并和其它写入放进同一
事务提交。撤销处理器在本模块底部通过 undo.register 注册：目标表在合并之后
没有被写入（__lsm_op 里没有更新的、未撤销的、非 undo/view 记录）时，把目标
表移入「已删除表」区；否则抛 StaleOperation，要求先撤销后续操作。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import audit, schema, undo
from .models import StaleOperation
from .schema import ColumnSpec
from .util import utcnow_iso

# 受管表自带的四列，业务列不能与之重名。
AUX_NAMES = ("id", "created_at", "updated_at", "deleted_at")

# 撤销时这些审计 kind 不阻塞对合并的撤销：undo 记录本身、视图配置不改变
# 合并出来的数据行。
_NON_BLOCKING_KINDS = ("undo", "view")

_SAVEPOINT = "lsm_merge"


@dataclass
class _Column:
    """一个目标列：显示名、逻辑类型，以及每张源表里对应的源列名。"""

    name: str
    type: str
    sources: dict = field(default_factory=dict)
    types: dict = field(default_factory=dict)

    @property
    def conflicting(self) -> bool:
        return len(set(self.types.values())) > 1


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _normalize(name: object) -> str:
    """列名归一口径：去掉首尾空格并忽略大小写。"""
    return str(name).strip().lower()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    return any(row[1] == column for row in rows)


def _active_count(conn: sqlite3.Connection, table: str) -> int:
    """源表里未软删的行数。"""
    sql = f"SELECT COUNT(*) FROM {_quote(table)}"
    if _has_column(conn, table, "deleted_at"):
        sql += ' WHERE "deleted_at" IS NULL'
    return int(conn.execute(sql).fetchone()[0])


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])


def _resolve_tables(conn: sqlite3.Connection, tables) -> list[str]:
    """校验并返回受管表的登记名（大小写以库为准）。

    受管表 = 真实存在 + 在 __lsm_table 里登记为 active。回收站里的表、
    未登记的外部表都不能参与合并。
    """
    resolved = []
    for table in tables:
        name = schema._ensure_user_table(conn, str(table).strip())
        row = conn.execute(
            'SELECT "status" FROM "__lsm_table" WHERE "table_name" = ?', (name,)
        ).fetchone()
        if row is None or row[0] != "active":
            raise ValueError(f"表未登记为受管表：{table}")
        resolved.append(name)
    return resolved


def _check_target(conn: sqlite3.Connection, target) -> str:
    """目标表名必须是空着的新名字。"""
    name = str(target).strip()
    if not name:
        raise ValueError("目标表名不能为空")
    schema._reject_reserved_name(name)
    if schema._name_taken(conn, name):
        raise ValueError(f"目标表已存在：{name}")
    return name


def _check_column_name(name: str) -> str:
    if not name:
        raise ValueError("列名不能为空")
    if name.lower() in AUX_NAMES:
        raise ValueError(f"列名与系统列冲突：{name}")
    return name


def _fields_by_key(conn: sqlite3.Connection, table: str) -> dict:
    """按归一列名索引该表的业务字段，保留字段的原始顺序。"""
    return {_normalize(field.name): field for field in schema.get_fields(conn, table)}


def _columns_from_union(sources: list[str], fields: dict) -> list[_Column]:
    """无映射时的列对齐：按首次出现顺序取所有源表业务列的并集。"""
    order: list[str] = []
    merged: dict[str, _Column] = {}
    for table in sources:
        for key, field in fields[table].items():
            column = merged.get(key)
            if column is None:
                column = merged[key] = _Column(
                    name=_check_column_name(field.name.strip()),
                    type=field.type,
                )
                order.append(key)
            column.sources[table] = field.name
            column.types[table] = field.type
    return [merged[key] for key in order]


def _columns_from_mapping(mapping, sources: list[str], fields: dict) -> list[_Column]:
    """按「目标列 -> {源表: 源列}」生成目标列，顺序即映射的键顺序。"""
    if not isinstance(mapping, dict):
        raise ValueError("列映射必须是字典")
    known = {table.lower(): table for table in sources}
    columns: list[_Column] = []
    seen: set = set()
    for raw_target, spec in mapping.items():
        name = _check_column_name(str(raw_target).strip())
        key = _normalize(name)
        if key in seen:
            raise ValueError(f"列映射中存在重复的目标列：{name}")
        seen.add(key)
        if not isinstance(spec, dict):
            raise ValueError(f"列映射「{name}」必须指明每张源表用哪一列")
        column = _Column(name=name, type="")
        for raw_table, raw_source in spec.items():
            table = known.get(str(raw_table).strip().lower())
            if table is None:
                raise ValueError(f"列映射中的表不在合并范围内：{raw_table}")
            field = fields[table].get(_normalize(raw_source))
            if field is None:
                raise ValueError(f"表 {table} 没有列：{raw_source}")
            column.sources[table] = field.name
            column.types[table] = field.type
        if not column.sources:
            raise ValueError(f"列映射「{name}」没有指向任何源列")
        # 类型取 tables 顺序中第一张包含该列的源表，保证与并集口径一致。
        column.type = next(column.types[table] for table in sources if table in column.types)
        columns.append(column)
    return columns


def _analyze(conn: sqlite3.Connection, tables, target, mapping=None):
    """校验输入并算出目标列、源表行数与类型冲突。"""
    names = [str(table).strip() for table in tables]
    if len(names) < 2:
        raise ValueError("合并至少需要 2 张表")
    sources = _resolve_tables(conn, names)
    target = _check_target(conn, target)
    fields = {table: _fields_by_key(conn, table) for table in sources}
    if mapping:
        columns = _columns_from_mapping(mapping, sources, fields)
    else:
        columns = _columns_from_union(sources, fields)
    counts = [(table, _active_count(conn, table)) for table in sources]
    conflicts = [
        {"column": column.name, "types": dict(column.types)}
        for column in columns
        if column.conflicting
    ]
    return target, columns, counts, conflicts


def plan_merge(conn: sqlite3.Connection, tables, target, mapping=None) -> dict:
    """预览合并结果，不写任何数据。

    返回 {"columns", "conflicts", "rows", "sources"}：

    - columns：目标列名列表，顺序即建表顺序；
    - conflicts：类型不一致的列，形如
      {"column": "备注", "types": {"一月": "text", "二月": "number"}}；
    - rows：合并后的总行数（只计未软删行）；
    - sources：每张源表的登记名与参与合并的行数。
    """
    _, columns, counts, conflicts = _analyze(conn, tables, target, mapping)
    return {
        "columns": [column.name for column in columns],
        "conflicts": conflicts,
        "rows": sum(count for _, count in counts),
        "sources": [{"table": table, "rows": count} for table, count in counts],
    }


def _copy_rows(
    conn: sqlite3.Connection, target: str, columns: list[_Column], counts
) -> None:
    """逐源表把未软删行插入目标表；缺列填 NULL，不做类型转换。"""
    now = utcnow_iso()
    head = f'INSERT INTO {_quote(target)} ("created_at", "updated_at"'
    if columns:
        head += ", " + ", ".join(_quote(column.name) for column in columns)
    head += ")"
    for table, _ in counts:
        values = ", ".join(
            _quote(column.sources[table]) if table in column.sources else "NULL"
            for column in columns
        )
        select = "SELECT ?, ?" + (f", {values}" if columns else "")
        sql = f"{head} {select} FROM {_quote(table)}"
        if _has_column(conn, table, "deleted_at"):
            sql += ' WHERE "deleted_at" IS NULL'
        conn.execute(sql, (now, now))


def merge_tables(
    conn: sqlite3.Connection, tables, target, mapping=None, source: str = "web"
) -> int:
    """把 tables 合并进新表 target，返回 merge 审计 op id。

    不提交事务：调用方（路由/MCP）负责 commit，使合并与其它写入原子。
    失败时回滚到本函数自己的保存点，且若事务由本函数开启则整体回滚。
    """
    target, columns, counts, conflicts = _analyze(conn, tables, target, mapping)
    expected = sum(count for _, count in counts)

    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    conn.execute(f'SAVEPOINT "{_SAVEPOINT}"')
    try:
        op = audit.record(
            conn,
            "merge",
            target,
            source,
            {
                "target": target,
                "tables": [table for table, _ in counts],
                "columns": [column.name for column in columns],
                "conflicts": conflicts,
                "rows": expected,
            },
        )
        schema.create_table(
            conn, target, [ColumnSpec(column.name, column.type) for column in columns]
        )
        _copy_rows(conn, target, columns, counts)
        actual = _table_count(conn, target)
        if actual != expected:
            raise ValueError(f"合并行数校验失败：预期 {expected} 行，实际 {actual} 行")
    except BaseException:
        conn.execute(f'ROLLBACK TO "{_SAVEPOINT}"')
        conn.execute(f'RELEASE "{_SAVEPOINT}"')
        if started:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute(f'RELEASE "{_SAVEPOINT}"')
    return op


def _has_later_ops(conn: sqlite3.Connection, target: str, op_id: int) -> bool:
    """目标表在合并之后是否还有未撤销的数据写入。

    undo 是解除阻塞的方式，view 只改配置，两者都不算。
    """
    placeholders = ", ".join("?" for _ in _NON_BLOCKING_KINDS)
    row = conn.execute(
        'SELECT 1 FROM "__lsm_op" WHERE "id" > ? AND "table_name" = ?'
        f' AND "undone_by" IS NULL AND "kind" NOT IN ({placeholders}) LIMIT 1',
        (op_id, target, *_NON_BLOCKING_KINDS),
    ).fetchone()
    return row is not None


def _undo_merge(conn: sqlite3.Connection, op: dict, source: str):
    """撤销合并：把目标表移入「已删除表」区；被后续写入改过则拒绝。"""
    payload = op.get("payload") or {}
    target = payload.get("target") or op.get("table")
    if not target:
        raise StaleOperation("操作记录缺少目标表名")
    if _has_later_ops(conn, target, op["id"]):
        raise StaleOperation("合并后的表已被修改，请先撤销后续操作")
    return undo.trash_table(conn, target, base_op=op["id"])


undo.register("merge", _undo_merge)
