"""分页读取与分组汇总。"""

from __future__ import annotations

import sqlite3

from .models import Group, PageResult, ViewConfig
from .query import build_where, page_sql
from .schema import get_fields
from .types import cast_expr

_AGGREGATE_FNS = ("sum", "avg", "min", "max", "count")


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    return any(row[1] == column for row in rows)


def _has_rowid(conn: sqlite3.Connection, table: str) -> bool:
    """判断普通表是否有 rowid；WITHOUT ROWID 表只能靠扫描顺序。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None and "WITHOUT ROWID" not in (row[0] or "").upper()


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
        tiebreaker = '"id"'
    elif _has_rowid(conn, table):
        tiebreaker = "rowid"
    else:
        tiebreaker = None

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
