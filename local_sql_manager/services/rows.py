"""分页读取、分组汇总与行级写入。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from . import audit
from .models import ConflictError, Group, PageResult, ViewConfig
from .query import build_where, page_sql
from .schema import get_fields
from .types import cast_expr, coerce
from .util import utcnow_iso

_AGGREGATE_FNS = ("sum", "avg", "min", "max", "count")


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    return any(row[1] == column for row in rows)


def _has_rowid(conn: sqlite3.Connection, table: str) -> bool:
    """用一次空查询判断 rowid 是否可用；视图与 WITHOUT ROWID 表会失败。"""
    try:
        conn.execute(f"SELECT rowid FROM {_quote(table)} LIMIT 0")
    except sqlite3.OperationalError:
        return False
    return True


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """按主键序号返回主键列名；无主键时为空。"""
    rows = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    ordered = sorted((row[5], row[1]) for row in rows if row[5])
    return [name for _, name in ordered]


def _conditions(view: ViewConfig, fields: list, soft_delete: bool) -> tuple[list, list]:
    where, params = build_where(view.filters, fields)
    conditions = ['"deleted_at" IS NULL'] if soft_delete else []
    if where:
        conditions.append(f"({where})")
    return conditions, params


def _aggregate_expr(aggregate: dict, by_name: dict) -> str | None:
    if not isinstance(aggregate, dict):
        return None
    field = by_name.get(aggregate.get("column"))
    fn = aggregate.get("fn")
    if field is None or fn not in _AGGREGATE_FNS:
        return None
    if fn == "count":
        return f"COUNT({_quote(field.name)})"
    return f"{fn.upper()}({cast_expr(field.name, field.type)})"


def _load_groups(
    conn: sqlite3.Connection,
    table: str,
    view: ViewConfig,
    fields: list,
    soft_delete: bool,
) -> tuple:
    column = view.group.get("column") if isinstance(view.group, dict) else None
    by_name = {field.name: field for field in fields}
    if column not in by_name:
        return ()

    expressions = [_aggregate_expr(agg, by_name) for agg in view.aggregates or []]
    select = [
        f'{_quote(column)} AS "key"',
        'COUNT(*) AS "count"',
    ]
    select.extend(
        f'NULL AS "agg{index}"' if expr is None else f'{expr} AS "agg{index}"'
        for index, expr in enumerate(expressions)
    )

    conditions, params = _conditions(view, fields, soft_delete)
    sql = f"SELECT {', '.join(select)} FROM {_quote(table)}"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f' GROUP BY {_quote(column)} ORDER BY {_quote(column)} ASC'

    return tuple(
        Group(key=row[0], count=row[1], aggregates=tuple(row[2:]))
        for row in conn.execute(sql, params).fetchall()
    )


def get_page(
    conn: sqlite3.Connection,
    table: str,
    view: ViewConfig,
    page: int = 1,
    page_size: int = 100,
) -> PageResult:
    """读取一页数据；total 与 groups 均按整个筛选集统计。"""
    view = view or ViewConfig()
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size)
    except (TypeError, ValueError):
        page_size = 100
    page = max(1, page)
    page_size = min(1000, max(1, page_size))

    fields = get_fields(conn, table)
    soft_delete = _has_column(conn, table, "deleted_at")
    # 没有 id 列时退回 rowid，避免 ORDER BY "id" 被 SQLite 当成常量而静默失效。
    if _has_column(conn, table, "id"):
        tiebreaker = "id"
    elif _has_rowid(conn, table):
        tiebreaker = "rowid"
    else:
        tiebreaker = _pk_columns(conn, table) or None

    sql, params = page_sql(
        table,
        fields,
        view,
        page_size,
        (page - 1) * page_size,
        soft_delete=soft_delete,
        tiebreaker=tiebreaker,
    )
    cursor = conn.execute(sql, params)
    names = [description[0] for description in cursor.description]
    rows = [dict(zip(names, row)) for row in cursor.fetchall()]

    conditions, count_params = _conditions(view, fields, soft_delete)
    count_sql = f"SELECT COUNT(*) FROM {_quote(table)}"
    if conditions:
        count_sql += " WHERE " + " AND ".join(conditions)
    total = conn.execute(count_sql, count_params).fetchone()[0]

    groups = _load_groups(conn, table, view, fields, soft_delete)

    return PageResult(
        rows=rows,
        total=total,
        groups=groups,
        page=page,
        page_size=page_size,
    )


# --- 写入 -------------------------------------------------------------
# 每次写入都在同一事务内完成「读旧值 → 记审计 → 写数据」。本模块的写函数
# 不提交事务：调用方（路由/MCP/撤销）负责 commit，这样才能让多条服务调用
# 组成一个原子操作。

_AUX_WRITE_COLUMNS = ("id", "created_at", "updated_at", "deleted_at")


@contextmanager
def _write_txn(conn: sqlite3.Connection):
    """保证写入在一个事务里完成，但不在本函数内提交。

    调用方已有事务时只开保存点，失败回滚到保存点，不破坏调用方之前的写入；
    否则自己开启事务并保持到调用方 commit/rollback。
    """
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN")
    conn.execute('SAVEPOINT "lsm_rows_write"')
    try:
        yield
    except BaseException:
        conn.execute('ROLLBACK TO "lsm_rows_write"')
        conn.execute('RELEASE "lsm_rows_write"')
        if started:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute('RELEASE "lsm_rows_write"')


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        row[1] for row in conn.execute(f"PRAGMA table_info({_quote(table)})")
    ]


def _writable_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        column
        for column in _table_columns(conn, table)
        if column not in _AUX_WRITE_COLUMNS
    ]


def _as_id(value: object) -> object:
    """把 JSON 传来的字符串主键还原成整数；非数字主键保持原样。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def _fetch_rows(
    conn: sqlite3.Connection, table: str, ids: list
) -> dict:
    """按 id 取整行，返回 {id: dict}。"""
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    cursor = conn.execute(
        f'SELECT * FROM {_quote(table)} WHERE "id" IN ({placeholders})', ids
    )
    names = [description[0] for description in cursor.description]
    return {
        row["id"]: row
        for row in (
            dict(zip(names, values)) for values in cursor.fetchall()
        )
    }


def _coerce_value(value: object, field) -> object:
    return value if field is None else coerce(value, field.type)


def _prepare_values(
    conn: sqlite3.Connection, table: str, values: dict
) -> dict:
    """校验列名并按逻辑类型转换取值。"""
    writable = set(_writable_columns(conn, table))
    fields = {field.name: field for field in get_fields(conn, table)}
    prepared = {}
    for column, value in values.items():
        if column not in writable:
            raise ValueError(f"未知列：{column}")
        prepared[column] = _coerce_value(value, fields.get(column))
    return prepared


def insert_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: list[dict],
    source: str = "web",
) -> int | None:
    """插入若干行，返回 __lsm_op.id；空批返回 None 且不记审计。"""
    rows = list(rows or [])
    if not rows:
        return None
    now = utcnow_iso()
    with _write_txn(conn):
        payload_rows = []
        for item in rows:
            values = _prepare_values(conn, table, dict(item or {}))
            columns = ["created_at", "updated_at"] + list(values)
            sql = (
                f'INSERT INTO {_quote(table)} '
                f'({", ".join(_quote(column) for column in columns)}) '
                f'VALUES ({", ".join("?" for _ in columns)})'
            )
            params = [now, now] + list(values.values())
            cursor = conn.execute(sql, params)
            inserted_id = cursor.lastrowid
            after = {"id": inserted_id, "created_at": now, "updated_at": now}
            after.update(values)
            payload_rows.append(
                {"id": inserted_id, "before": None, "after": after}
            )
        return audit.record(
            conn, "insert", table, source, {"rows": payload_rows}
        )


def update_cells(
    conn: sqlite3.Connection,
    table: str,
    changes: dict,
    expected: dict | None = None,
    source: str = "web",
) -> int | None:
    """按行更新若干单元格，使用 updated_at 做乐观锁。

    expected 传 None 表示显式关闭并发保护（仅供内部脚本）；传 dict 时必须为
    每个参与修改的行提供 updated_at，缺失或为 None 抛 ValueError。
    任一行版本不一致时抛 ConflictError（携带最新行），整批不做任何写入。
    空批返回 None 且不记审计。
    """
    requested = {
        _as_id(key): dict(value or {})
        for key, value in (changes or {}).items()
    }
    # 空变更行不算参与修改，直接从批次里剔除。
    requested = {rid: values for rid, values in requested.items() if values}
    ids = list(requested)
    if not ids:
        return None
    expected_map = None
    if expected is not None:
        expected_map = {
            _as_id(key): value for key, value in expected.items()
        }

    if not _has_column(conn, table, "updated_at"):
        raise ValueError(f"表 {table} 没有 updated_at 列，无法启用并发保护")

    now = utcnow_iso()
    with _write_txn(conn):
        latest = _fetch_rows(conn, table, ids)
        prepared = []
        conflicts = []
        for rid in ids:
            row = latest.get(rid)
            if row is None:
                conflicts.append({"id": rid})
                continue
            if expected_map is not None:
                want = expected_map.get(rid)
                if want is None:
                    raise ValueError("缺少 updated_at 版本，无法安全写入")
                if str(row["updated_at"]) != str(want):
                    conflicts.append(row)
                    continue
            values = _prepare_values(conn, table, requested[rid])
            prepared.append((rid, row, values))

        if conflicts:
            raise ConflictError(rows=conflicts)

        # updated_at 必须进前后值：撤销 update 时要把行还原到操作前的完整
        # 状态，否则随后撤销更早的 delete/restore 会因 updated_at 不一致被判过期。
        payload_rows = [
            {
                "id": rid,
                "before": {
                    **{column: row[column] for column in values},
                    "updated_at": row["updated_at"],
                },
                "after": {**values, "updated_at": now},
            }
            for rid, row, values in prepared
        ]
        op = audit.record(
            conn, "update", table, source, {"rows": payload_rows}
        )

        for rid, row, values in prepared:
            if not values:
                continue
            assignments = ", ".join(
                f"{_quote(column)} = ?" for column in values
            )
            cursor = conn.execute(
                f'UPDATE {_quote(table)} SET {assignments}, "updated_at" = ? '
                f'WHERE "id" = ? AND "updated_at" = ?',
                list(values.values()) + [now, rid, row["updated_at"]],
            )
            if cursor.rowcount == 0:
                fresh = _fetch_rows(conn, table, [rid])
                raise ConflictError(rows=[fresh.get(rid, {"id": rid})])
    return op


def soft_delete(
    conn: sqlite3.Connection,
    table: str,
    ids: list,
    source: str = "web",
) -> int | None:
    """把若干行移入回收站（写 deleted_at）；空批或全部已在回收站返回 None。"""
    if not _has_column(conn, table, "deleted_at"):
        raise ValueError(f"表 {table} 没有 deleted_at 列，无法软删除")
    normalized = [_as_id(value) for value in ids or []]
    if not normalized:
        return None
    now = utcnow_iso()
    with _write_txn(conn):
        latest = _fetch_rows(conn, table, normalized)
        targets = [
            (rid, latest[rid])
            for rid in normalized
            if rid in latest and latest[rid]["deleted_at"] is None
        ]
        if not targets:
            return None
        payload_rows = [
            {
                "id": rid,
                "before": dict(row),
                "after": {**row, "deleted_at": now, "updated_at": now},
            }
            for rid, row in targets
        ]
        op = audit.record(conn, "delete", table, source, {"rows": payload_rows})
        for rid, _row in targets:
            conn.execute(
                f'UPDATE {_quote(table)} SET "deleted_at" = ?, "updated_at" = ? '
                f'WHERE "id" = ? AND "deleted_at" IS NULL',
                (now, now, rid),
            )
    return op


def restore_rows(
    conn: sqlite3.Connection,
    table: str,
    ids: list,
    source: str = "web",
) -> int | None:
    """把回收站里的行恢复（清空 deleted_at）；空批或无命中返回 None。

    唯一索引冲突时抛 ValueError（提示「唯一」），整批恢复回滚。
    """
    if not _has_column(conn, table, "deleted_at"):
        raise ValueError(f"表 {table} 没有 deleted_at 列，无法恢复")
    normalized = [_as_id(value) for value in ids or []]
    if not normalized:
        return None
    now = utcnow_iso()
    with _write_txn(conn):
        latest = _fetch_rows(conn, table, normalized)
        targets = [
            (rid, latest[rid])
            for rid in normalized
            if rid in latest and latest[rid]["deleted_at"] is not None
        ]
        if not targets:
            return None
        payload_rows = [
            {
                "id": rid,
                "before": dict(row),
                "after": {**row, "deleted_at": None, "updated_at": now},
            }
            for rid, row in targets
        ]
        op = audit.record(
            conn, "restore", table, source, {"rows": payload_rows}
        )
        try:
            for rid, _row in targets:
                conn.execute(
                    f'UPDATE {_quote(table)} SET "deleted_at" = NULL, '
                    f'"updated_at" = ? WHERE "id" = ?',
                    (now, rid),
                )
        except sqlite3.IntegrityError as exc:
            # 部分唯一索引下恢复可能与现存行撞值，给界面一个可读的错误而不是 500。
            raise ValueError(f"恢复失败：唯一性约束冲突（{exc}）") from exc
    return op


def purge_rows(
    conn: sqlite3.Connection,
    table: str,
    ids: list,
    source: str = "web",
) -> int | None:
    """彻底删除回收站中的行，审计标记 irreversible；空批返回 None。

    只能删除已软删的行：批次里出现活跃行时整批拒绝并抛 ValueError。
    """
    if not _has_column(conn, table, "deleted_at"):
        raise ValueError(f"表 {table} 没有 deleted_at 列，无法彻底删除")
    normalized = [_as_id(value) for value in ids or []]
    if not normalized:
        return None
    with _write_txn(conn):
        latest = _fetch_rows(conn, table, normalized)
        active = [
            rid
            for rid in normalized
            if rid in latest and latest[rid]["deleted_at"] is None
        ]
        if active:
            raise ValueError("只能彻底删除回收站中的行")
        targets = [(rid, latest[rid]) for rid in normalized if rid in latest]
        if not targets:
            return None
        payload_rows = [
            {"id": rid, "before": dict(row), "after": None}
            for rid, row in targets
        ]
        op = audit.record(
            conn,
            "purge",
            table,
            source,
            {"rows": payload_rows},
            irreversible=True,
        )
        for rid, _row in targets:
            conn.execute(f'DELETE FROM {_quote(table)} WHERE "id" = ?', (rid,))
    return op

