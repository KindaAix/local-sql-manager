"""视图的创建、改名、删除与设为默认。

视图配置的保存（筛选/排序/隐藏列/列序/分组/汇总）走本模块的
POST /api/views/<id>；列宽与列序的增量保存仍由 grid_api 的
POST /api/views/<id>/config 负责，两者不重复。

所有接口只接受 JSON，成功 commit、失败 rollback；视图属于哪张表由服务层
记录，路由不缓存任何状态。
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from local_sql_manager.services import views

from .grid_api import (
    _ApiError,
    _json_body,
    _require_managed,
    _require_table,
    _run,
)

bp = Blueprint("views_api", __name__, url_prefix="/api/views")

MAX_VIEW_NAME = 64

# 面板配置里这几项必须是数组；中文名用于错误提示。
_CONFIG_LISTS = {
    "filters": "筛选条件",
    "sorts": "排序",
    "hidden": "隐藏列",
    "order": "列序",
    "aggregates": "汇总",
}


def _checked_config(raw: object) -> dict:
    """校验 config 的容器结构；字段名引用是否有效交给服务层裁剪。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _ApiError("config 必须是对象")
    for key, label in _CONFIG_LISTS.items():
        if key in raw and not isinstance(raw[key], list):
            raise _ApiError(f"{label}必须是数组")
    if "widths" in raw and not isinstance(raw["widths"], dict):
        raise _ApiError("列宽必须是 {列: 像素} 对象")
    if raw.get("group") is not None and not isinstance(raw.get("group"), dict):
        raise _ApiError("分组必须是对象")
    return raw


def _checked_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _ApiError("视图名称不能为空")
    name = raw.strip()
    if len(name) > MAX_VIEW_NAME:
        raise _ApiError(f"视图名称不能超过 {MAX_VIEW_NAME} 个字符")
    return name


@bp.post("")
def create():
    """新建视图：body = {table, name, config}，返回 201 与新 id。"""

    def write(conn):
        body = _json_body()
        table = body.get("table")
        if not isinstance(table, str) or not table.strip():
            raise _ApiError("请指定表名")
        _require_table(conn, table)
        _require_managed(conn, table)
        name = _checked_name(body.get("name"))
        config = _checked_config(body.get("config"))
        return views.create_view(conn, table, name, config)

    return _run(write, lambda conn, view_id: (jsonify({"ok": True, "id": view_id}), 201))


@bp.post("/<int:view_id>")
def update(view_id: int):
    """更新视图：body 可带 name / config，至少给一个。"""

    def write(conn):
        body = _json_body()
        row = views._fetch(conn, view_id)
        if row is None:
            raise _ApiError("视图不存在", 404)
        raw_name = body.get("name")
        name = None if raw_name is None else _checked_name(raw_name)
        raw_config = body.get("config")
        if name is None and raw_config is None:
            raise _ApiError("没有需要保存的修改")
        config = (
            None if raw_config is None else _checked_config(raw_config)
        )
        current = views.load_config(conn, view_id)
        if config is None:
            merged = current
        else:
            # 面板只提交筛选/排序/隐藏列/列序/分组/汇总；没带的列宽沿用现值，
            # 避免把用户拖过的列宽重置。
            merged = dict(config)
            if "widths" not in merged:
                merged["widths"] = dict(current.widths or {})
        views.update_view(conn, view_id, merged, None if name is None else name)
        return view_id

    return _run(write, lambda conn, _view: jsonify({"ok": True}))


@bp.delete("/<int:view_id>")
def delete(view_id: int):
    """删除视图；默认视图不可删除（服务层抛 ValueError）。"""

    def write(conn):
        views.delete_view(conn, view_id)
        return view_id

    return _run(write, lambda conn, _view: jsonify({"ok": True}))


@bp.post("/<int:view_id>/default")
def make_default(view_id: int):
    """把视图设为该表的默认视图。"""

    def write(conn):
        _json_body()
        row = views._fetch(conn, view_id)
        if row is None:
            raise _ApiError("视图不存在", 404)
        views.set_default(conn, row[1], view_id)
        return view_id

    return _run(write, lambda conn, _view: jsonify({"ok": True}))
