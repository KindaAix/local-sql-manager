"""视图配置的增删改查。

视图只是元数据：每张表至少有一个默认视图「全部记录」，它由 list_views
在首次读取时懒创建，且不可删除。所有写函数都不提交事务，由调用方在
同一事务内 commit；视图配置变化不写审计（见设计文档 §7.3）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, fields as dc_fields

from .models import ViewConfig
from .schema import get_fields

DEFAULT_VIEW_NAME = "全部记录"

_COLUMNS = (
    'SELECT "id", "table_name", "name", "config_json", "position", "is_default"'
    ' FROM "__lsm_view"'
)
_CONFIG_FIELDS = {item.name for item in dc_fields(ViewConfig)}


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _require_table(conn: sqlite3.Connection, table: str) -> None:
    if not _table_exists(conn, table):
        raise ValueError(f"表不存在：{table}")


def _as_config(config: ViewConfig | dict) -> ViewConfig:
    """允许路由层直接传 JSON 反序列化出的 dict。"""
    if isinstance(config, dict):
        return ViewConfig(**{k: v for k, v in config.items() if k in _CONFIG_FIELDS})
    if not isinstance(config, ViewConfig):
        raise ValueError("视图配置类型不正确")
    return config


def _prune(config: ViewConfig, table: str, conn: sqlite3.Connection) -> ViewConfig:
    """丢掉引用已不存在列的配置项，避免删列后留下悬空引用。"""
    known = {field.name for field in get_fields(conn, table)}

    def known_column(item: object) -> bool:
        return isinstance(item, dict) and item.get("column") in known

    hidden = [name for name in config.hidden or [] if name in known]
    order = [name for name in config.order or [] if name in known]
    widths = {
        name: width
        for name, width in (config.widths or {}).items()
        if name in known
    }
    group = config.group if known_column(config.group) else None
    return ViewConfig(
        filters=[item for item in config.filters or [] if known_column(item)],
        sorts=[item for item in config.sorts or [] if known_column(item)],
        hidden=hidden,
        order=order,
        widths=widths,
        group=group,
        aggregates=[item for item in config.aggregates or [] if known_column(item)],
    )


def _encode(config: ViewConfig) -> str:
    return json.dumps(asdict(config), ensure_ascii=False)


def _decode(raw: str | None) -> ViewConfig:
    if not raw:
        return ViewConfig()
    data = json.loads(raw)
    if not isinstance(data, dict):
        return ViewConfig()
    return ViewConfig(**{k: v for k, v in data.items() if k in _CONFIG_FIELDS})


def _to_dict(row: sqlite3.Row | tuple) -> dict:
    return {
        "id": row[0],
        "table": row[1],
        "name": row[2],
        "config": _decode(row[3]),
        "position": row[4],
        "is_default": bool(row[5]),
    }


def _fetch(conn: sqlite3.Connection, view_id: int):
    return conn.execute(_COLUMNS + ' WHERE "id" = ?', (view_id,)).fetchone()


def default_view(table: str) -> dict:
    """默认视图的展示信息，用于界面在无视图时占位。"""
    return {"name": DEFAULT_VIEW_NAME, "config": ViewConfig()}


def list_views(conn: sqlite3.Connection, table: str) -> list[dict]:
    """按 position 列出视图；该表没有任何视图时补建默认视图。

    表不存在时返回空列表。本函数可能写入一条默认视图，不提交事务。
    """
    if not _table_exists(conn, table):
        return []
    rows = conn.execute(
        _COLUMNS + ' WHERE "table_name" = ? ORDER BY "position", "id"', (table,)
    ).fetchall()
    if not rows:
        conn.execute(
            """
            INSERT INTO "__lsm_view"
                ("table_name", "name", "config_json", "position", "is_default")
            VALUES (?, ?, ?, 0, 1)
            """,
            (table, DEFAULT_VIEW_NAME, _encode(ViewConfig())),
        )
        rows = conn.execute(
            _COLUMNS + ' WHERE "table_name" = ? ORDER BY "position", "id"', (table,)
        ).fetchall()
    return [_to_dict(row) for row in rows]


def create_view(
    conn: sqlite3.Connection, table: str, name: str, config: ViewConfig | dict
) -> int:
    """在表末尾新增视图，返回 __lsm_view.id；表还没有默认视图时新视图即默认。"""
    _require_table(conn, table)
    name = (name or "").strip()
    if not name:
        raise ValueError("视图名称不能为空")
    cleaned = _prune(_as_config(config), table, conn)
    row = conn.execute(
        'SELECT COALESCE(MAX("position"), -1) + 1,'
        ' COALESCE(MAX("is_default"), 0) FROM "__lsm_view" WHERE "table_name" = ?',
        (table,),
    ).fetchone()
    position, has_default = row[0], row[1]
    cur = conn.execute(
        """
        INSERT INTO "__lsm_view"
            ("table_name", "name", "config_json", "position", "is_default")
        VALUES (?, ?, ?, ?, ?)
        """,
        (table, name, _encode(cleaned), position, 0 if has_default else 1),
    )
    return cur.lastrowid


def update_view(
    conn: sqlite3.Connection,
    view_id: int,
    config: ViewConfig | dict,
    name: str | None = None,
) -> None:
    """替换视图配置；name 不为 None 时同时重命名。不提交事务。"""
    row = _fetch(conn, view_id)
    if row is None:
        raise ValueError("视图不存在")
    cleaned = _prune(_as_config(config), row[1], conn)
    if name is None:
        conn.execute(
            'UPDATE "__lsm_view" SET "config_json" = ? WHERE "id" = ?',
            (_encode(cleaned), view_id),
        )
        return
    name = name.strip()
    if not name:
        raise ValueError("视图名称不能为空")
    conn.execute(
        'UPDATE "__lsm_view" SET "config_json" = ?, "name" = ? WHERE "id" = ?',
        (_encode(cleaned), name, view_id),
    )


def delete_view(conn: sqlite3.Connection, view_id: int) -> None:
    """删除视图；默认视图不可删除。不提交事务。"""
    row = _fetch(conn, view_id)
    if row is None:
        raise ValueError("视图不存在")
    if row[5]:
        raise ValueError("默认视图不能删除")
    conn.execute('DELETE FROM "__lsm_view" WHERE "id" = ?', (view_id,))


def set_default(conn: sqlite3.Connection, table: str, view_id: int) -> None:
    """把某视图设为该表默认视图。不提交事务。"""
    row = _fetch(conn, view_id)
    if row is None:
        raise ValueError("视图不存在")
    if row[1] != table:
        raise ValueError("视图不属于该表")
    conn.execute(
        'UPDATE "__lsm_view" SET "is_default" = 0 WHERE "table_name" = ?', (table,)
    )
    conn.execute('UPDATE "__lsm_view" SET "is_default" = 1 WHERE "id" = ?', (view_id,))


def load_config(conn: sqlite3.Connection, view_id: int) -> ViewConfig:
    """读取视图配置；视图不存在时返回空配置。"""
    row = _fetch(conn, view_id)
    if row is None:
        return ViewConfig()
    return _decode(row[3])
