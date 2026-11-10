"""网格页面：左表列表、主网格、分组头与分页条。

本模块只负责把查询参数转成服务层调用，并把结果交给模板；不碰 SQL 细节。
"""

from __future__ import annotations

import math

from flask import Blueprint, redirect, render_template, request, url_for

from local_sql_manager.app import get_conn
from local_sql_manager.services import rows, schema, views
from local_sql_manager.services.models import Field

bp = Blueprint("grid", __name__)

PAGE_SIZES = (50, 100, 200)
DEFAULT_PAGE_SIZE = 100

# 受管表固定带这四个辅助列；界面上只展示后两个，id 与 deleted_at 不出现。
HIDDEN_AUX_COLUMNS = ("id", "deleted_at")
AUX_DISPLAY_COLUMNS = ("created_at", "updated_at")

AGGREGATE_LABELS = {
    "sum": "求和",
    "avg": "平均",
    "min": "最小",
    "max": "最大",
    "count": "计数",
}


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _parse_int(raw: object) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_page(raw: object) -> int:
    value = _parse_int(raw)
    return value if value is not None and value > 0 else 1


def _parse_size(raw: object) -> int:
    value = _parse_int(raw)
    return value if value in PAGE_SIZES else DEFAULT_PAGE_SIZE


def _table_columns(conn, table: str) -> list[str]:
    cursor = conn.execute(f"PRAGMA table_info({_quote(table)})")
    return [row[1] for row in cursor.fetchall()]


def _is_managed(conn, table: str) -> bool:
    row = conn.execute(
        'SELECT 1 FROM "__lsm_table" WHERE "table_name" = ?', (table,)
    ).fetchone()
    return row is not None


def _business_fields(conn, table: str) -> list:
    """受管表用登记的业务字段；未登记的表按物理列兜底成只读列。"""
    fields = schema.get_fields(conn, table)
    if fields:
        return fields
    skip = set(HIDDEN_AUX_COLUMNS) | set(AUX_DISPLAY_COLUMNS)
    return [
        Field(name=name, type="text")
        for name in _table_columns(conn, table)
        if name not in skip
    ]


def _display_columns(conn, table: str, fields: list, config) -> list:
    """业务列（按字段顺序、跳过隐藏）+ 只读辅助列。"""
    hidden = set(config.hidden or [])
    columns = [field for field in fields if field.name not in hidden]
    physical = set(_table_columns(conn, table))
    for name in AUX_DISPLAY_COLUMNS:
        if name in physical:
            columns.append(Field(name=name, type="text"))
    return columns


def _select_view(options: list, raw: object) -> dict:
    """挑出要用的视图；参数非法或属于别的表时回落到默认视图。"""
    wanted = _parse_int(raw)
    if wanted is not None:
        for item in options:
            if item["id"] == wanted:
                return item
    for item in options:
        if item["is_default"]:
            return item
    return options[0]


def display_text(value: object) -> str:
    """单元格文本：空值留空，整数形态的小数去掉 .0。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@bp.app_template_filter("cell")
def _cell_filter(value: object) -> str:
    return display_text(value)


def _aggregate_labels(config, fields: list) -> list[str]:
    """与 Group.aggregates 一一对齐的汇总标题；无效配置留空串占位。"""
    by_name = {field.name: field for field in fields}
    labels = []
    for item in config.aggregates or []:
        if not isinstance(item, dict):
            labels.append("")
            continue
        column = item.get("column")
        function = item.get("fn")
        if column in by_name and function in AGGREGATE_LABELS:
            labels.append(f"{column} {AGGREGATE_LABELS[function]}")
        else:
            labels.append("")
    return labels


def _group_label(value: object) -> str:
    return "（空）" if value is None else display_text(value)


def _build_blocks(result, config, fields: list) -> list | None:
    """按本页行序切成组块，供模板插入组头行；未分组时返回 None。"""
    group = config.group if isinstance(config.group, dict) else None
    column = group.get("column") if group else None
    if not column or not result.groups:
        return None

    labels = _aggregate_labels(config, fields)
    by_key = {item.key: item for item in result.groups}
    blocks: list[dict] = []
    for row in result.rows:
        key = row.get(column)
        if not blocks or key != blocks[-1]["key"]:
            info = by_key.get(key)
            blocks.append(
                {
                    "key": key,
                    "label": _group_label(key),
                    "count": info.count if info is not None else 0,
                    "values": list(info.aggregates) if info is not None else [],
                    "continued": False,
                    "rows": [],
                }
            )
        blocks[-1]["rows"].append(row)

    # 组跨页时只有本页第一个组块需要标「（续）」：它是上一页未完的那一组。
    head = blocks[0]
    head["continued"] = result.page > 1 and head["count"] > len(head["rows"])

    for block in blocks:
        block["aggregates"] = [
            {
                "label": label,
                "value": block["values"][index] if index < len(block["values"]) else None,
            }
            for index, label in enumerate(labels)
            if label
        ]
    return blocks


@bp.get("/")
def index():
    conn = get_conn()
    tables = schema.list_tables(conn)
    if not tables:
        return render_template("grid.html", tables=[], table=None, error=None)
    return redirect(url_for("grid.table_view", table=tables[0]))


@bp.get("/t/<table>/")
def table_view(table: str):
    conn = get_conn()
    tables = schema.list_tables(conn)
    if table not in tables:
        return (
            render_template(
                "grid.html",
                error=f"表「{table}」不存在，可能已被删除或改名。",
                tables=tables,
                table=None,
            ),
            404,
        )

    options = views.list_views(conn, table)
    active = _select_view(options, request.args.get("view"))
    # 视图配置一律重新读一遍：list_views 只为列表命名，配置以 load_config 为准。
    config = views.load_config(conn, active["id"])

    size = _parse_size(request.args.get("size"))
    result = rows.get_page(conn, table, config, _parse_page(request.args.get("page")), size)

    fields = _business_fields(conn, table)
    columns = _display_columns(conn, table, fields, config)
    blocks = _build_blocks(result, config, fields)

    if result.rows:
        first_row = (result.page - 1) * size + 1
        last_row = first_row + len(result.rows) - 1
    else:
        first_row = last_row = 0

    return render_template(
        "grid.html",
        error=None,
        table=table,
        tables=tables,
        views=options,
        active_view=active,
        page=result,
        fields=fields,
        columns=columns,
        grouped=blocks is not None,
        blocks=blocks or [],
        groups=result.groups,
        size=size,
        page_sizes=PAGE_SIZES,
        total_pages=max(1, math.ceil(result.total / size)),
        first_row=first_row,
        last_row=last_row,
        managed=_is_managed(conn, table),
    )