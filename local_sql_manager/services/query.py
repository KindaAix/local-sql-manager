"""按视图配置生成筛选、排序与分页 SQL；参数一律绑定。"""

from __future__ import annotations

import math

from .models import Field, ViewConfig

# 每种逻辑字段可用的操作符，界面据此渲染筛选菜单。
OPS = {
    "text": ("contains", "not_contains", "eq", "neq", "empty", "not_empty"),
    "url": ("contains", "not_contains", "eq", "neq", "empty", "not_empty"),
    "select": ("eq", "neq", "empty", "not_empty"),
    "multiselect": ("contains", "not_contains", "empty", "not_empty"),
    "number": ("eq", "neq", "gt", "gte", "lt", "lte", "empty", "not_empty"),
    "date": ("eq", "neq", "gt", "gte", "lt", "lte", "empty", "not_empty"),
    "checkbox": ("is_true", "is_false"),
}

_COMPARISONS = {
    "eq": "=",
    "neq": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _to_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date_part(value: object) -> str:
    """把日期取值归一成 YYYY-MM-DD；认不出时退回前 10 个字符。"""
    text = str(value).strip()
    head = text.split(" ")[0].split("T")[0].replace("/", "-")
    parts = head.split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        year, month, day = (int(part) for part in parts)
        return f"{year:04d}-{month:02d}-{day:02d}"
    return text[:10]


def _clause(field: Field, op: str, value: object) -> tuple[str, list] | None:
    column = _quote(field.name)
    if op == "empty":
        return f"({column} IS NULL OR TRIM(CAST({column} AS TEXT)) = '')", []
    if op == "not_empty":
        return f"({column} IS NOT NULL AND TRIM(CAST({column} AS TEXT)) <> '')", []
    if op == "is_true":
        return f"COALESCE(CAST({column} AS INTEGER), 0) = 1", []
    if op == "is_false":
        return f"COALESCE(CAST({column} AS INTEGER), 0) = 0", []
    if _is_blank(value):
        return None
    if op in ("contains", "not_contains"):
        keyword = "LIKE" if op == "contains" else "NOT LIKE"
        pattern = "%" + _escape_like(str(value).strip()) + "%"
        return f"{column} {keyword} ? ESCAPE '\\'", [pattern]
    comparison = _COMPARISONS[op]
    if field.type == "number":
        number = _to_number(value)
        if number is None:
            return None
        return f"CAST({column} AS REAL) {comparison} ?", [number]
    if field.type == "date":
        return f"substr({column}, 1, 10) {comparison} ?", [_date_part(value)]
    return f"{column} {comparison} ?", [str(value)]


def build_where(filters: list, fields: list) -> tuple[str, list]:
    """把筛选配置转成不带 WHERE 的 SQL 片段；未知列/操作符/取值一律忽略。"""
    by_name = {field.name: field for field in fields}
    clauses = []
    params = []
    for item in filters or []:
        if not isinstance(item, dict):
            continue
        column = item.get("column")
        field = by_name.get(column)
        if field is None:
            continue
        op = item.get("op")
        if op not in OPS.get(field.type, ()):
            continue
        built = _clause(field, op, item.get("value"))
        if built is None:
            continue
        clauses.append(built[0])
        params.extend(built[1])
    return " AND ".join(clauses), params


def build_order(sorts: list, group: dict | None) -> str:
    """把排序与分组转成不带 ORDER BY 的 SQL 片段，分组列总在最前。"""
    parts = []
    group_column = group.get("column") if isinstance(group, dict) else None
    if group_column:
        parts.append(f"{_quote(group_column)} ASC")
    for item in sorts or []:
        if not isinstance(item, dict):
            continue
        column = item.get("column")
        if not column:
            continue
        direction = "DESC" if str(item.get("dir", "asc")).lower() == "desc" else "ASC"
        parts.append(f"{_quote(column)} {direction}")
    return ", ".join(parts)


def page_sql(
    table: str,
    fields: list,
    view: ViewConfig,
    limit: int,
    offset: int,
    soft_delete: bool = True,
    tiebreaker: str | None = '"id"',
) -> tuple[str, list]:
    """生成一页数据的查询；默认过滤软删行，并以 tiebreaker 保证跨页顺序稳定。"""
    names = {field.name for field in fields}
    where, params = build_where(view.filters, fields)
    conditions = []
    if soft_delete:
        conditions.append('"deleted_at" IS NULL')
    if where:
        conditions.append(f"({where})")

    sql = f"SELECT * FROM {_quote(table)}"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    group = (
        view.group
        if isinstance(view.group, dict) and view.group.get("column") in names
        else None
    )
    sorts = [
        item
        for item in view.sorts or []
        if isinstance(item, dict) and item.get("column") in names
    ]
    order_parts = []
    order = build_order(sorts, group)
    if order:
        order_parts.append(order)
    if tiebreaker:
        order_parts.append(f"{tiebreaker} ASC")
    if order_parts:
        sql += " ORDER BY " + ", ".join(order_parts)
    sql += " LIMIT ? OFFSET ?"
    return sql, params + [limit, offset]
