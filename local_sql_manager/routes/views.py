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
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _ApiError("视图名称不能为空")
        config = body.get("config")
        if config is not None and not isinstance(config, dict):
            raise _ApiError("config 必须是对象")
        return views.create_view(conn, table, name, config or {})

    return _run(write, lambda conn, view_id: (jsonify({"ok": True, "id": view_id}), 201))


@bp.post("/<int:view_id>")
def update(view_id: int):
    """更新视图：body 可带 name / config，至少给一个。"""

    def write(conn):
        body = _json_body()
        row = views._fetch(conn, view_id)
        if row is None:
            raise _ApiError("视图不存在", 404)
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise _ApiError("视图名称必须是文本")
        config = body.get("config")
        if config is not None and not isinstance(config, dict):
            raise _ApiError("config 必须是对象")
        if name is None and config is None:
            raise _ApiError("没有需要保存的修改")
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
