"""多表合并：把多张受管表的行汇入一张新表。

语义
====

- 合并只新建目标表，源表的数据与元数据都不改动。
- 只复制源表中未软删（deleted_at IS NULL）的行；软删行留在源表里，
  不会被「复活」到目标表。
- 目标表是普通受管表：由 schema.create_table 建表，因此自动带
  id / created_at / updated_at / deleted_at 四列与业务列索引；
  行 id 重新分配，created_at / updated_at 取合并时刻。

源表校验
========

- tables 必须是非字符串的可迭代表名列表，至少 2 张；表名去首尾空格后按
  大小写不敏感去重，重复直接报 ValueError，避免把同一张表复制两遍。
- 每张表必须是已登记且 status 为 active 的受管表；回收站里的表与未登记的
  外部表都不能参与合并。
- 元数据里登记的源列必须在物理表里真实存在，否则报错——否则 SQLite 的
  双引号兼容（DQS）会把拼错的列名当字符串字面量静默写进目标表。

列对齐
======

列名先做 strip().casefold() 归一化再比较（与字段管理口径一致），目标列的
显示名取首次出现的原始写法（去掉首尾空格）。没有给 mapping 时，目标列是各
源表业务列的并集，顺序为按 tables 顺序扫描时各列首次出现的顺序；某张源表
没有该列时补 NULL（不做类型强转）。

mapping 的口径是「目标列 -> {源表: 源列}」::

    mapping = {"金额": {"一月": "金额", "二月": "Amount"}}

键是要出现在目标表里的列名，顺序即目标列顺序；值指明每张源表用哪一列
填充它。给了 mapping 时只有映射里列出的目标列会进入新表，没有列出的源表
该列填 NULL。mapping 不能是空字典；同一张源表不能用大小写变体写两次。
plan_merge 与 merge_tables 共用同一套解析，所以确认页看到的列就是最终建出
的列。

类型与选项
==========

目标列的逻辑类型取 tables 顺序中第一张包含该列的源表的字段类型；单选/多选列
的 options 取各源表选项的并集（按首次出现顺序去重）并写入目标字段元数据。
同一列在源表间类型或选项不一致时记入 conflicts，但不阻止合并——物理列按
首个类型建，值依赖 SQLite 列亲和性写入，不做显式转换。

审计与撤销
==========

merge_tables 写一条 kind="merge" 的审计，payload 含 target / tables /
columns / conflicts / rows，且不提交事务：调用方把合并和其它写入放进同一
事务提交。撤销处理器在本模块底部通过 undo.register 注册：目标表在合并之后
没有被写入时，把目标表移入「已删除表」区；否则抛 StaleOperation，要求先撤销
后续操作。是否「被写入」复用 undo.has_later_ops 的口径（动态豁免已注册的
元数据 kind），不在本模块重复实现。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import audit, schema, undo
from .models import StaleOperation
from .schema import ColumnSpec
from .util import utcnow_iso

# 受管表自带的四列，业务列不能与之重名；以 schema 的定义为准。
AUX_NAMES = tuple(name for name, _ in schema.AUX_COLUMNS)

_SAVEPOINT = "lsm_merge"


@dataclass
class _Column:
    """一个目标列：显示名、逻辑类型、选项，以及每张源表里对应的源列名。"""

    name: str
    type: str
    options: tuple = ()
    sources: dict = field(default_factory=dict)
    types: dict = field(default_factory=dict)
    table_options: dict = field(default_factory=dict)

    @property
    def conflicting(self) -> bool:
        return len(set(self.types.values())) > 1 or len(
            {frozenset(options) for options in self.table_options.values()}
        ) > 1


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _normalize(name: object) -> str:
    """列名归一口径：去掉首尾空格并忽略大小写。"""
    return str(name).strip().casefold()


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


def _dedupe(sources: list[str]) -> list[str]:
    """按大小写不敏感去重；同一张表出现两次会让行被复制两遍。"""
    seen: set = set()
    unique: list[str] = []
    for table in sources:
        key = table.casefold()
        if key in seen:
            raise ValueError(f"源表重复：{table}")
        seen.add(key)
        unique.append(table)
    return unique


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
    if name.casefold() in AUX_NAMES:
        raise ValueError(f"列名与系统列冲突：{name}")
    return name


def _union_options(current: tuple, extra) -> tuple:
    """把 extra 里没见过的选项按原顺序追加到 current。"""
    merged = list(current)
    for option in extra or ():
        if option not in merged:
            merged.append(option)
    return tuple(merged)


def _fields_by_key(conn: sqlite3.Connection, table: str) -> dict:
    """按归一列名索引该表的业务字段，保留字段的原始顺序。

    归一后撞键说明同一张表里有只差大小写/空格的列，无法确定对齐关系，
    直接报错而不是静默丢掉一列。
    """
    fields: dict = {}
    for field in schema.get_fields(conn, table):
        key = _normalize(field.name)
        if key in fields:
            raise ValueError(
                f"表 {table} 的列名归一化后重复：{fields[key].name} / {field.name}"
            )
        fields[key] = field
    return fields


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
            column.table_options[table] = tuple(field.options)
            column.options = _union_options(column.options, field.options)
    return [merged[key] for key in order]


def _columns_from_mapping(mapping, sources: list[str], fields: dict) -> list[_Column]:
    """按「目标列 -> {源表: 源列}」生成目标列，顺序即映射的键顺序。"""
    if not isinstance(mapping, dict):
        raise ValueError("列映射必须是字典")
    if not mapping:
        raise ValueError("列映射不能为空")
    known = {table.casefold(): table for table in sources}
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
            table = known.get(str(raw_table).strip().casefold())
            if table is None:
                raise ValueError(f"列映射中的表不在合并范围内：{raw_table}")
            if table in column.sources:
                raise ValueError(f"列映射「{name}」中源表重复：{raw_table}")
            field = fields[table].get(_normalize(raw_source))
            if field is None:
                raise ValueError(f"表 {table} 没有列：{raw_source}")
            column.sources[table] = field.name
            column.types[table] = field.type
            column.table_options[table] = tuple(field.options)
            column.options = _union_options(column.options, field.options)
        if not column.sources:
            raise ValueError(f"列映射「{name}」没有指向任何源列")
        # 类型取 tables 顺序中第一张包含该列的源表，保证与并集口径一致。
        column.type = next(column.types[table] for table in sources if table in column.types)
        columns.append(column)
    return columns


def _check_physical_columns(conn: sqlite3.Connection, columns: list[_Column]) -> None:
    """元数据里登记的源列必须真的存在。

    SQLite 对双引号标识符有 DQS 兼容：列名拼错时会退化成字符串字面量，
    SELECT 静默返回列名本身，行数校验拦不住这种伪值。
    """
    for column in columns:
        for table, source_column in column.sources.items():
            if not _has_column(conn, table, source_column):
                raise ValueError(
                    f"表 {table} 缺少列：{source_column}"
                    "（元数据与实际结构不一致，请重新启用编辑）"
                )


def _conflict_entry(column: _Column) -> dict:
    entry = {"column": column.name, "types": dict(column.types)}
    if len({frozenset(options) for options in column.table_options.values()}) > 1:
        entry["options"] = {
            table: list(options) for table, options in column.table_options.items()
        }
    return entry


def _analyze(conn: sqlite3.Connection, tables, target, mapping=None):
    """校验输入并算出目标列、源表行数与类型/选项冲突。"""
    if isinstance(tables, str) or not hasattr(tables, "__iter__"):
        raise ValueError("源表必须是表名列表")
    names = [str(table).strip() for table in tables]
    if len(names) < 2:
        raise ValueError("合并至少需要 2 张表")
    sources = _dedupe(_resolve_tables(conn, names))
    target = _check_target(conn, target)
    fields = {table: _fields_by_key(conn, table) for table in sources}
    if mapping is not None:
        columns = _columns_from_mapping(mapping, sources, fields)
    else:
        columns = _columns_from_union(sources, fields)
    _check_physical_columns(conn, columns)
    counts = [(table, _active_count(conn, table)) for table in sources]
    conflicts = [_conflict_entry(column) for column in columns if column.conflicting]
    return target, columns, counts, conflicts


def plan_merge(conn: sqlite3.Connection, tables, target, mapping=None) -> dict:
    """预览合并结果，不写任何数据。

    返回 {"columns", "conflicts", "rows", "sources"}：

    - columns：目标列名列表，顺序即建表顺序；
    - conflicts：类型或选项不一致的列，形如
      {"column": "备注", "types": {"一月": "text", "二月": "number"}}，
      选项不一致时另带 "options": {表名: [选项...]}；
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
    校验、建表、拷贝与行数核对都在同一事务的保存点内完成；失败时回滚到
    该保存点，且若事务由本函数开启则整体回滚。
    """
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    conn.execute(f'SAVEPOINT "{_SAVEPOINT}"')
    try:
        target, columns, counts, conflicts = _analyze(conn, tables, target, mapping)
        expected = sum(count for _, count in counts)
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
            conn,
            target,
            [
                ColumnSpec(column.name, column.type, column.options)
                for column in columns
            ],
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


def _undo_merge(conn: sqlite3.Connection, op: dict, source: str):
    """撤销合并：把目标表移入「已删除表」区；被后续写入改过则拒绝。"""
    payload = op.get("payload") or {}
    target = payload.get("target") or op.get("table")
    if not target:
        raise StaleOperation("操作记录缺少目标表名")
    if undo.has_later_ops(conn, target, op["id"]):
        raise StaleOperation("合并后的表已被修改，请先撤销后续操作")
    return undo.trash_table(conn, target, base_op=op["id"])


undo.register("merge", _undo_merge)
