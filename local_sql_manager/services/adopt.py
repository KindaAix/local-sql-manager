"""外部表启用编辑：把非本应用建的表重建成受管表。

设计见 spec §7.7：未登记在 __lsm_table 的表只读；用户点「启用编辑」时按
12 步重建（关闭外键 → 建新表 → 拷数据 → 删旧表 → 改名 → 重建索引/触发器
→ foreign_key_check → 恢复外键），补齐 id / created_at / updated_at /
deleted_at，并把原有 UNIQUE 索引改写为只约束未删除行的部分索引。

本模块沿用服务层约定：先 audit.record，再执行 DDL；不主动提交事务，由调用
方在同一事务内提交。唯一例外是连接启用了外键且调用方没有开启事务时——重建
必须先关外键，而 PRAGMA foreign_keys 只在事务外生效，因此这种场景下本函数
会在成功收尾时提交自身事务再恢复外键（此时没有调用方的未提交数据）。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import audit
from .models import AdoptError
from .schema import DEFAULT_FIELD_WIDTH, _rename_table_strict, ensure_indexes
from .types import infer_type
from .util import utcnow_iso

# 重建期间的新表名。以 __lsm_ 开头，界面不会把它当成用户表。
TEMP_TABLE = "__lsm_adopt_new"

# 受管表的附加列，按固定顺序排在最前。
AUX_NAMES = ("id", "created_at", "updated_at", "deleted_at")

_CREATED_AT_DEFAULT = "DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"

# deleted_at 里常见的「未删除」哨兵值；重建时统一归一化为 NULL。
SENTINEL_RULE = "deleted_at_sentinel_v1"
SENTINEL_VALUES = (
    "",
    "0",
    "0.0",
    "false",
    "no",
    "n",
    "0000-00-00",
    "0000-00-00 00:00:00",
)
# SQLite 的 trim 默认只去空格；这里把 ASCII 空白字符集交给 trim，等价于
# Python 的 strip（只去两端，不误伤内部的空白/单词）。
_WHITESPACE_CHARS = (
    "char(32) || char(9) || char(10) || char(11) || char(12) || char(13)"
)
_SENTINEL_IN = ", ".join(f"'{value}'" for value in SENTINEL_VALUES)


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _normalized_text(name: str) -> str:
    """把列转成文本、去两端空白并小写，用于 deleted_at 哨兵判断。"""
    return f"lower(trim(CAST({_quote(name)} AS TEXT), {_WHITESPACE_CHARS}))"


@dataclass
class _Column:
    """PRAGMA table_xinfo 的一行。"""

    name: str
    type: str
    notnull: bool
    default: str | None
    pk: int
    hidden: int = 0


@dataclass
class _Index:
    """重建后要恢复的一个索引。"""

    name: str
    unique: bool
    origin: str
    columns: list[str]
    column_defs: list[str]
    sql: str | None
    create_sql: str | None = None


@dataclass
class _ForeignKey:
    """重建后要保留的一条外键约束。"""

    parent: str
    columns: list[str]
    parent_columns: list[str]
    on_update: str
    on_delete: str
    match: str


@dataclass
class _Plan:
    table: str
    columns: list[_Column]
    pk: _Column | None
    added: list[str]
    renamed_pk: dict | None
    indexes: list[_Index]
    triggers: list[str]
    foreign_keys: list[_ForeignKey]
    checks: list[str]
    strict: bool
    row_count: int
    sentinel_count: int
    fk_before: int
    reason: str
    by_name: dict = field(default_factory=dict)


# --- 预检 -------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> list[_Column]:
    """读取列定义；优先 table_xinfo（能识别生成列），失败则退回 table_info。"""
    rows = conn.execute(f"PRAGMA table_xinfo({_quote(table)})").fetchall()
    if rows:
        return [
            _Column(
                name=row[1],
                type=row[2] or "",
                notnull=bool(row[3]),
                default=row[4],
                pk=row[5] or 0,
                hidden=row[6] if len(row) > 6 else 0,
            )
            for row in rows
        ]
    rows = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    return [
        _Column(
            name=row[1],
            type=row[2] or "",
            notnull=bool(row[3]),
            default=row[4],
            pk=row[5] or 0,
        )
        for row in rows
    ]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _is_managed(conn: sqlite3.Connection, table: str) -> bool:
    """__lsm_table 里只要存在该表的任何登记行（含回收站状态）就不再采纳。"""
    if not _has_table(conn, "__lsm_table"):
        return False
    row = conn.execute(
        'SELECT 1 FROM "__lsm_table" WHERE "table_name" = ?', (table,)
    ).fetchone()
    return row is not None


def _resolve(conn: sqlite3.Connection, table: str) -> tuple[str, str, str | None]:
    """返回 (实际表名, 类型, 建表 SQL)；不存在时抛 AdoptError。"""
    row = conn.execute(
        "SELECT name, type, sql FROM sqlite_master"
        " WHERE name = ? COLLATE NOCASE AND type IN ('table', 'view')",
        (str(table),),
    ).fetchone()
    if row is None:
        raise AdoptError(f"表不存在：{table}")
    return row[0], row[1], row[2]


def _unique_index_name(table: str, columns: list[str]) -> str:
    """为原 UNIQUE 约束生成可读且不易撞名的部分索引名。"""
    parts = [f"{len(column)}_{column}" for column in columns]
    return f"__lsm_uniq_{len(table)}_{table}_{'_'.join(parts)}"


def _where_position(sql: str) -> int:
    """找出括号/字符串之外的关键字 WHERE，避免列名碰巧叫 where 时误判。"""
    depth = 0
    quote = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "\"'\x60":
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif (
            depth == 0
            and sql[index : index + 5].upper() == "WHERE"
            and (index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] == "_"))
            and (
                index + 5 == len(sql)
                or not (sql[index + 5].isalnum() or sql[index + 5] == "_")
            )
        ):
            return index
        index += 1
    return -1


def _as_partial(sql: str) -> str:
    """把唯一索引 SQL 改写为部分索引：仅约束未删除行。

    已有 WHERE 的索引把原条件与新条件用 AND 连接，避免收紧其它语义。
    """
    position = _where_position(sql)
    if position < 0:
        return sql.rstrip().rstrip(";") + " WHERE deleted_at IS NULL"
    head = sql[:position].rstrip()
    condition = sql[position + 5 :].strip().rstrip(";")
    if re.fullmatch(r'"?deleted_at"?\s+IS\s+NULL', condition, re.I):
        return f"{head} WHERE deleted_at IS NULL"
    return f"{head} WHERE ({condition}) AND deleted_at IS NULL"


def _rename_in_sql(sql: str, old: str, new: str) -> str:
    """把索引/触发器 SQL 里的旧列名替换为新列名（仅用于主键改名）。"""
    quoted = [
        '"' + re.escape(old) + '"',
        "\x60" + re.escape(old) + "\x60",
        r"\[" + re.escape(old) + r"\]",
    ]
    bare = r"(?<![\w\"])" + re.escape(old) + r"(?![\w\"])"
    pattern = re.compile("|".join(quoted + [bare]))
    return pattern.sub('"' + new.replace('"', '""') + '"', sql)


def _check_constraints(sql: str | None) -> list[str]:
    """从原始建表 SQL 中提取所有 CHECK(...) 表达式并转成表级约束。

    只为保留校验语义，不解析整条 DDL；字符串字面量里的 CHECK 会被跳过。
    """
    if not sql:
        return []
    found: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char in "\"'\x60" or char == "[":
            quote = "]" if char == "[" else char
            index += 1
            while index < length:
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    break
                index += 1
            index += 1
            continue
        is_check = sql[index : index + 5].upper() == "CHECK" and (
            index == 0 or not (sql[index - 1].isalnum() or sql[index - 1] == "_")
        ) and (
            index + 5 == length
            or not (sql[index + 5].isalnum() or sql[index + 5] == "_")
        )
        if not is_check:
            index += 1
            continue
        cursor = index + 5
        while cursor < length and sql[cursor].isspace():
            cursor += 1
        if cursor >= length or sql[cursor] != "(":
            index = cursor
            continue
        start = cursor
        depth = 0
        quote = None
        while cursor < length:
            char = sql[cursor]
            if quote:
                if char == quote:
                    if cursor + 1 < length and sql[cursor + 1] == quote:
                        cursor += 2
                        continue
                    quote = None
            elif char in "\"'\x60":
                quote = char
            elif char == "[":
                quote = "]"
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        found.append("CHECK " + sql[start : cursor + 1])
        index = cursor + 1
    return found


def _plan_indexes(conn: sqlite3.Connection, table: str) -> list[_Index]:
    """读取索引定义，保留 collation 与 DESC，供重建唯一索引时复用。"""
    indexes: list[_Index] = []
    rows = conn.execute(f"PRAGMA index_list({_quote(table)})").fetchall()
    for row in rows:
        name, unique, origin = row[1], bool(row[2]), row[3]
        if origin == "pk":
            continue
        xinfo = conn.execute(f"PRAGMA index_xinfo({_quote(name)})").fetchall()
        if xinfo:
            keys = [info for info in xinfo if info[5]]
        else:  # 极老版本没有 index_xinfo
            keys = [
                (info[0], info[1], info[2], 0, "BINARY", 1)
                for info in conn.execute(
                    f"PRAGMA index_info({_quote(name)})"
                ).fetchall()
            ]
        columns = [info[2] for info in keys if info[2] is not None]
        column_defs = []
        for info in keys:
            if info[2] is None:  # 表达式索引只能靠原 SQL 重放
                continue
            chunk = _quote(info[2])
            collation = info[4]
            if collation and str(collation).upper() != "BINARY":
                chunk += f" COLLATE {_quote(collation)}"
            if info[3]:
                chunk += " DESC"
            column_defs.append(chunk)
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        ).fetchone()
        indexes.append(
            _Index(
                name=name,
                unique=unique,
                origin=origin,
                columns=columns,
                column_defs=column_defs,
                sql=None if sql_row is None else sql_row[0],
            )
        )
    return indexes


def _prepare(conn: sqlite3.Connection, table: str) -> _Plan:
    """校验并生成重建计划；不满足条件时抛 AdoptError。"""
    if str(table).lower().startswith(("__lsm_", "sqlite_")):
        raise AdoptError(f"不能对内部表启用编辑：{table}")
    for meta in ("__lsm_table", "__lsm_op"):
        if not _has_table(conn, meta):
            raise AdoptError(f"库缺少元数据表 {meta}，无法启用编辑")
    resolved, kind, create_sql = _resolve(conn, table)
    if kind == "view":
        raise AdoptError("视图不能启用编辑")
    if create_sql and str(create_sql).lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
        raise AdoptError("虚拟表不能启用编辑")
    if _is_managed(conn, resolved):
        raise AdoptError("该表已启用编辑")
    if create_sql and re.search(r"\)\s*WITHOUT\s+ROWID", create_sql, re.I):
        raise AdoptError("WITHOUT ROWID 表暂不支持启用编辑")

    columns = _columns(conn, resolved)
    if any(column.hidden in (2, 3) for column in columns):
        raise AdoptError("含生成列的表暂不支持启用编辑，请先移除生成列")

    by_name = {column.name.lower(): column for column in columns}
    pk_columns = sorted((column.pk, column) for column in columns if column.pk)
    pk: _Column | None = None
    renamed_pk: dict | None = None
    added: list[str] = []

    if len(pk_columns) > 1:
        raise AdoptError("表有复合主键，无法启用编辑；请先改成单列整数主键")
    if pk_columns:
        pk = pk_columns[0][1]
        if pk.type.strip().upper() != "INTEGER":
            raise AdoptError("主键不是整数类型，无法安全启用编辑")
        if pk.name != "id":
            other = by_name.get("id")
            if other is not None and other is not pk:
                raise AdoptError("已有名为 id 的列但不是主键，请先重命名该列")
            renamed_pk = {"from": pk.name, "to": "id"}
    else:
        if "id" in by_name:
            raise AdoptError("已有名为 id 的列但不是整数主键，请先重命名该列")
        added.append("id")
    for name in ("created_at", "updated_at", "deleted_at"):
        if name not in by_name:
            added.append(name)

    indexes = _plan_indexes(conn, resolved)
    for index in indexes:
        if index.origin == "u":
            index.name = _unique_index_name(resolved, index.columns)
            column_sql = ", ".join(index.column_defs)
            suffix = " WHERE deleted_at IS NULL" if index.unique else ""
            index.create_sql = (
                f"CREATE {'UNIQUE ' if index.unique else ''}INDEX {_quote(index.name)}"
                f" ON {_quote(resolved)} ({column_sql}){suffix}"
            )
        elif index.sql:
            index.create_sql = index.sql
        else:
            continue
        if renamed_pk:
            index.create_sql = _rename_in_sql(
                index.create_sql, renamed_pk["from"], "id"
            )
        if index.unique and index.origin == "c":
            index.create_sql = _as_partial(index.create_sql)

    triggers = [
        row[0]
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?"
            " AND sql IS NOT NULL ORDER BY name",
            (resolved,),
        )
    ]
    if renamed_pk:
        triggers = [
            _rename_in_sql(sql, renamed_pk["from"], "id") for sql in triggers
        ]

    def canonical(name: str | None) -> str | None:
        if name is None:
            return None
        if renamed_pk and name == renamed_pk["from"]:
            return "id"
        return name.lower() if name.lower() in set(AUX_NAMES) else name

    groups: dict[int, list] = {}
    for row in conn.execute(f"PRAGMA foreign_key_list({_quote(resolved)})"):
        groups.setdefault(row[0], []).append(row)
    foreign_keys = []
    for identifier in sorted(groups):
        rows = sorted(groups[identifier], key=lambda item: item[1])
        self_reference = str(rows[0][2]).lower() == resolved.lower()
        foreign_keys.append(
            _ForeignKey(
                parent=rows[0][2],
                columns=[canonical(row[3]) for row in rows],
                parent_columns=[
                    canonical(row[4]) if self_reference else row[4] for row in rows
                ],
                on_update=rows[0][5] or "NO ACTION",
                on_delete=rows[0][6] or "NO ACTION",
                match=rows[0][7] or "NONE",
            )
        )

    row_count = conn.execute(f"SELECT COUNT(*) FROM {_quote(resolved)}").fetchone()[0]
    sentinel_count = 0
    if "deleted_at" not in added:
        deleted_column = by_name["deleted_at"]
        sentinel_count = conn.execute(
            f"SELECT COUNT(*) FROM {_quote(resolved)}"
            f" WHERE {_quote(deleted_column.name)} IS NOT NULL"
            f" AND {_normalized_text(deleted_column.name)}"
            f" IN ({_SENTINEL_IN})"
        ).fetchone()[0]
    fk_before = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    strict = bool(create_sql and re.search(r"\)\s*STRICT\s*$", create_sql, re.I))
    if strict:
        for name in ("created_at", "updated_at", "deleted_at"):
            column = by_name.get(name)
            if column is not None and column.type.strip().upper() != "TEXT":
                raise AdoptError(
                    f"STRICT 表的 {name} 列必须是 TEXT（当前为"
                    f" {column.type or '无类型'}），无法启用编辑"
                )

    parts = []
    if pk is None:
        parts.append("将新增 id 列")
    elif renamed_pk:
        parts.append(f"将主键 {renamed_pk['from']} 重命名为 id")
    else:
        parts.append("将复用现有整数主键 id")
    extra_added = [
        name for name in added if not (pk is None and name == "id")
    ]
    if extra_added:
        parts.append("新增 " + "、".join(extra_added) + " 列")
    unique = [index for index in indexes if index.unique]
    if unique:
        names = "、".join(
            "(" + ", ".join(index.columns) + ")" for index in unique
        )
        parts.append(f"唯一索引 {names} 将改为部分索引，仅约束未删除行")
    else:
        parts.append("重建后补建各列普通索引")
    if sentinel_count:
        parts.append(
            f"已有 {sentinel_count} 行 deleted_at 为空串、0（含 0.0）、false、"
            "no、n、零日期或纯空白，重建后视为未删除"
        )
    if create_sql and re.search(r"\bON\s+CONFLICT\b", create_sql, re.I):
        parts.append(
            "原表 UNIQUE 的 ON CONFLICT 策略无法用唯一索引表达，重建后冲突处理回到默认"
        )
    parts.append("列级 COLLATE 不会保留，比较与排序将按默认规则")

    checks = _check_constraints(create_sql)
    if renamed_pk:
        checks = [
            _rename_in_sql(check, renamed_pk["from"], "id") for check in checks
        ]

    return _Plan(
        table=resolved,
        columns=columns,
        pk=pk,
        added=added,
        renamed_pk=renamed_pk,
        indexes=indexes,
        triggers=triggers,
        foreign_keys=foreign_keys,
        checks=checks,
        strict=strict,
        row_count=row_count,
        sentinel_count=sentinel_count,
        fk_before=fk_before,
        reason="；".join(parts),
        by_name=by_name,
    )


def check_adoptable(conn: sqlite3.Connection, table: str) -> tuple[bool, str]:
    """判断外部表能否启用编辑，返回 (是否可采纳, 中文原因/计划说明)。"""
    try:
        plan = _prepare(conn, table)
    except AdoptError as exc:
        return False, str(exc)
    return True, plan.reason


# --- 重建 -------------------------------------------------------------


def _column_ddl(
    column: _Column,
    name: str | None = None,
    force_nullable: bool = False,
    drop_default: bool = False,
) -> str:
    """按列定义生成 DDL；name 用于把已有附加列收敛成规范小写名。"""
    parts = [_quote(column.name if name is None else name)]
    if column.type:
        parts.append(column.type)
    if column.notnull and not force_nullable:
        parts.append("NOT NULL")
    if column.default is not None and not drop_default:
        parts.append(f"DEFAULT {column.default}")
    return " ".join(parts)


def _references_foreign_keys(conn: sqlite3.Connection, table: str) -> bool:
    """表是否作为父表或子表参与外键；只有这种表重建时才必须关外键。"""
    if conn.execute(f"PRAGMA foreign_key_list({_quote(table)})").fetchall():
        return True
    lower = table.lower()
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ):
        name = row[0]
        if name == table:
            continue
        for fk in conn.execute(f"PRAGMA foreign_key_list({_quote(name)})").fetchall():
            if str(fk[2]).lower() == lower:
                return True
    return False


def _build_columns(plan: _Plan) -> tuple[list[str], list[str], list[str], list[Any]]:
    """生成新表的列定义与拷贝语句；返回 (定义, 目标列, 来源表达式, 参数)。"""
    now = utcnow_iso()
    added = set(plan.added)
    definitions = ['"id" INTEGER PRIMARY KEY AUTOINCREMENT']
    insert_columns: list[str] = []
    select_expressions: list[str] = []
    params: list[Any] = []

    if plan.pk is not None:
        insert_columns.append('"id"')
        select_expressions.append(_quote(plan.pk.name))

    if "created_at" in added:
        definitions.append('"created_at" TEXT NOT NULL ' + _CREATED_AT_DEFAULT)
        insert_columns.append('"created_at"')
        select_expressions.append("?")
        params.append(now)
    else:
        column = plan.by_name["created_at"]
        definitions.append(_column_ddl(column, "created_at"))
        insert_columns.append('"created_at"')
        select_expressions.append(f"COALESCE({_quote(column.name)}, ?)")
        params.append(now)

    if "updated_at" in added:
        definitions.append('"updated_at" TEXT NOT NULL ' + _CREATED_AT_DEFAULT)
        insert_columns.append('"updated_at"')
        select_expressions.append("?")
        params.append(now)
    else:
        column = plan.by_name["updated_at"]
        definitions.append(_column_ddl(column, "updated_at"))
        insert_columns.append('"updated_at"')
        select_expressions.append(f"COALESCE({_quote(column.name)}, ?)")
        params.append(now)

    if "deleted_at" in added:
        definitions.append('"deleted_at" TEXT')
        insert_columns.append('"deleted_at"')
        select_expressions.append("NULL")
    else:
        column = plan.by_name["deleted_at"]
        # deleted_at 必须可写 NULL，否则恢复行会失败；原 DEFAULT 常是 ''/0
        # 这类「未删除」哨兵，保留会让新插入的行一出生就被判为已删除。
        definitions.append(
            _column_ddl(
                column,
                "deleted_at",
                force_nullable=True,
                drop_default=True,
            )
        )
        insert_columns.append('"deleted_at"')
        select_expressions.append(
            f'CASE WHEN {_quote(column.name)} IS NULL THEN NULL'
            f" WHEN {_normalized_text(column.name)} IN ({_SENTINEL_IN})"
            " THEN NULL"
            f" ELSE {_quote(column.name)} END"
        )

    aux_names = set(AUX_NAMES)
    for column in plan.columns:
        if column.name.lower() in aux_names:
            continue
        if plan.pk is not None and column.name == plan.pk.name:
            continue
        definitions.append(_column_ddl(column))
        insert_columns.append(_quote(column.name))
        select_expressions.append(_quote(column.name))

    definitions.extend(_foreign_key_ddl(fk) for fk in plan.foreign_keys)
    definitions.extend(plan.checks)
    return definitions, insert_columns, select_expressions, params


def _foreign_key_ddl(foreign_key: _ForeignKey) -> str:
    """生成表级 FOREIGN KEY 子句，保留原表的外键关系。"""
    columns = ", ".join(_quote(column) for column in foreign_key.columns)
    target = _quote(foreign_key.parent)
    if any(foreign_key.parent_columns):
        target += " (" + ", ".join(
            _quote(column) for column in foreign_key.parent_columns
        ) + ")"
    parts = [f"FOREIGN KEY ({columns}) REFERENCES {target}"]
    if foreign_key.on_update.upper() != "NO ACTION":
        parts.append(f"ON UPDATE {foreign_key.on_update}")
    if foreign_key.on_delete.upper() != "NO ACTION":
        parts.append(f"ON DELETE {foreign_key.on_delete}")
    if foreign_key.match.upper() != "NONE":
        parts.append(f"MATCH {foreign_key.match}")
    return " ".join(parts)


def _create_indexes(conn: sqlite3.Connection, plan: _Plan) -> None:
    """按计划重建索引；UNIQUE 索引已在计划阶段改写为部分索引。"""
    for index in plan.indexes:
        if index.create_sql:
            conn.execute(index.create_sql)


def _recreate_triggers(conn: sqlite3.Connection, plan: _Plan) -> None:
    for sql in plan.triggers:
        conn.execute(sql)


def _infer_field_type(conn: sqlite3.Connection, table: str, column: str) -> str:
    values = []
    for row in conn.execute(
        f"SELECT {_quote(column)} FROM {_quote(table)}"
        f" WHERE {_quote(column)} IS NOT NULL LIMIT 200"
    ):
        value = row[0]
        if isinstance(value, (bytes, bytearray, memoryview)):
            continue
        values.append(str(value))
    return infer_type(values)[0]


def _register_metadata(conn: sqlite3.Connection, plan: _Plan) -> None:
    """登记 __lsm_table / __lsm_field，并补建各业务列的普通索引。"""
    if not _has_table(conn, "__lsm_table"):
        raise AdoptError("库缺少元数据表，无法登记为可编辑表")
    conn.execute('DELETE FROM "__lsm_table" WHERE "table_name" = ?', (plan.table,))
    conn.execute(
        'INSERT INTO "__lsm_table" ("table_name", "status", "created_at",'
        ' "source_file", "trashed_op") VALUES (?, ?, ?, NULL, NULL)',
        (plan.table, "active", utcnow_iso()),
    )
    conn.execute('DELETE FROM "__lsm_field" WHERE "table_name" = ?', (plan.table,))
    aux_names = set(AUX_NAMES)
    position = 0
    # 按重建后的真实列登记，避免把已重命名为 id 的原主键当成业务列。
    for column in _columns(conn, plan.table):
        if column.name.lower() in aux_names:
            continue
        conn.execute(
            'INSERT INTO "__lsm_field" ("table_name", "column", "type",'
            ' "options_json", "position", "width") VALUES (?, ?, ?, NULL, ?, ?)',
            (
                plan.table,
                column.name,
                _infer_field_type(conn, plan.table, column.name),
                position,
                DEFAULT_FIELD_WIDTH,
            ),
        )
        position += 1
    ensure_indexes(conn, plan.table)


def _payload(plan: _Plan) -> dict:
    return {
        "name": plan.table,
        "added_columns": list(plan.added),
        "reused_pk": None if plan.pk is None else plan.pk.name,
        "renamed_pk": plan.renamed_pk,
        "rewritten_indexes": [
            {"name": index.name, "columns": list(index.columns)}
            for index in plan.indexes
            if index.unique
        ],
        "indexes": [index.name for index in plan.indexes],
        "row_count": plan.row_count,
        "sentinel_count": plan.sentinel_count,
        "sentinel_rule": SENTINEL_RULE,
        "triggers": list(plan.triggers),
    }


def _rebuild(conn: sqlite3.Connection, plan: _Plan) -> None:
    old = plan.table
    definitions, insert_columns, select_expressions, params = _build_columns(plan)
    ddl = (
        f"CREATE TABLE {_quote(TEMP_TABLE)} (\n  "
        + ",\n  ".join(definitions)
        + "\n)"
    )
    if plan.strict:
        ddl += " STRICT"
    conn.execute(ddl)

    if insert_columns:
        conn.execute(
            f"INSERT INTO {_quote(TEMP_TABLE)}"
            f" ({', '.join(insert_columns)})"
            f" SELECT {', '.join(select_expressions)} FROM {_quote(old)}",
            params,
        )
    copied = conn.execute(
        f"SELECT COUNT(*) FROM {_quote(TEMP_TABLE)}"
    ).fetchone()[0]
    if copied != plan.row_count:
        raise AdoptError(f"重建前后行数不一致（{plan.row_count} → {copied}），已取消")

    conn.execute(f"DROP TABLE {_quote(old)}")
    # 临时表无人引用，正常走 modern；若库里有坏视图挡住重解析，
    # schema._rename_table_strict 会自动回退 legacy。
    _rename_table_strict(conn, TEMP_TABLE, old)

    _create_indexes(conn, plan)
    _recreate_triggers(conn, plan)
    _register_metadata(conn, plan)

    violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    if violations > plan.fk_before:
        raise AdoptError("重建后外键校验未通过，已取消")


def _restore_foreign_keys(conn: sqlite3.Connection) -> None:
    """确保连接回到事务外后重新启用外键。

    PRAGMA foreign_keys 在事务内无效，所以先把残留事务回滚掉；回滚失败也
    要继续尝试恢复，避免把连接留在「外键已关闭」的状态。
    """
    if conn.in_transaction:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    conn.execute("PRAGMA foreign_keys=ON")


def adopt_table(conn: sqlite3.Connection, table: str, source: str = "web") -> int:
    """按 12 步重建外部表并登记为受管表，返回 __lsm_op.id。

    事务约定：默认与其它服务层写函数一致——不提交事务，由调用方在同一事务
    内提交。**唯一的 commit 路径**是：连接已启用外键、目标表确实参与外键、
    且调用方没有开启外层事务。此时必须在事务外临时关闭 PRAGMA foreign_keys
    才能重建，而 Python/SQLite 无法在调用方提交后再帮它恢复 PRAGMA，所以本
    函数在成功收尾时自行提交这一操作，并在 finally 中确保恢复
    PRAGMA foreign_keys=ON（若回滚/提交异常则先回滚到 AUTOCOMMIT 再恢复）。
    调用方已在外层事务中且目标表参与外键时，本函数抛 AdoptError 而不是假装
    关闭了外键。
    """
    plan = _prepare(conn, table)

    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    # 只有参与外键的表才需要关掉约束；无关表在调用方事务里也能安全重建。
    toggle_fk = bool(foreign_keys) and _references_foreign_keys(conn, plan.table)
    if toggle_fk and conn.in_transaction:
        raise AdoptError("连接已启用外键且处于事务中，无法安全重建；请先提交当前事务")
    if toggle_fk:
        conn.execute("PRAGMA foreign_keys=OFF")

    started = not conn.in_transaction
    try:
        if started:
            conn.execute("BEGIN")
        conn.execute('SAVEPOINT "lsm_adopt"')
        try:
            op = audit.record(
                conn,
                "adopt",
                plan.table,
                source,
                _payload(plan),
                irreversible=True,
            )
            _rebuild(conn, plan)
        except BaseException:
            # 事务可能已被 SQLite 自动回滚，清理失败不能掩盖原始异常。
            try:
                conn.execute('ROLLBACK TO "lsm_adopt"')
                conn.execute('RELEASE "lsm_adopt"')
            except sqlite3.Error:
                pass
            raise
        conn.execute('RELEASE "lsm_adopt"')
        if toggle_fk:
            conn.commit()
    except BaseException:
        # 只回滚本函数自己开的顶层事务；调用方的事务交给调用方处理。
        if started:
            conn.rollback()
        raise
    finally:
        if toggle_fk:
            _restore_foreign_keys(conn)
    return op
