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


def _conditions(view: ViewConfig, fields: list, soft_delete: bool) -> tuple[list, list]:
    where, params = build_where(view.filters, fields)
    conditions = ['"deleted_at" IS NULL'] if soft_delete else []
    if where:
        conditions.append(f"({where})")
    return conditions, params


def _aggregate_expr(aggregate: dict, by_name: dict) -> str | None:
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
    column = (view.group or {}).get("column")
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
    page = max(1, int(page))
    page_size = max(1, int(page_size))
    fields = get_fields(conn, table)
    soft_delete = _has_column(conn, table, "deleted_at")

    sql, params = page_sql(
        table,
        fields,
        view,
        page_size,
        (page - 1) * page_size,
        soft_delete=soft_delete,
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
