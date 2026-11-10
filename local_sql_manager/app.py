"""Flask 应用工厂。"""

from __future__ import annotations

import sqlite3

from flask import Flask, current_app, g

from .services.library import Library

# 本地库的锁等待时间；界面是单用户，30 秒足够让并发写入排队。
CONNECT_TIMEOUT = 30


def get_conn() -> sqlite3.Connection:
    """返回当前请求使用的库连接。

    Web 服务以 threaded=True 运行，sqlite 连接不能跨线程共享，因此连接按
    应用上下文（每个请求）新建，请求结束时由 teardown 关闭；调用方负责
    commit/rollback。
    """
    conn = g.get("lsm_conn")
    if conn is not None:
        return conn
    path = current_app.config.get("LSM_DB_PATH")
    if path is None:
        raise RuntimeError("未绑定库文件")
    conn = sqlite3.connect(path, timeout=CONNECT_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    g.lsm_conn = conn
    return conn


def _close_conn(_exception: BaseException | None = None) -> None:
    conn = g.pop("lsm_conn", None)
    if conn is not None:
        conn.close()


def create_app(library: Library | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["LSM_DB_PATH"] = library.path if library is not None else None
    app.teardown_appcontext(_close_conn)

    @app.get("/healthz")
    def healthz() -> str:
        return "ok"

    # 蓝图在函数内导入：routes 反过来要从本模块取 get_conn，顶层导入会成环。
    from .routes.grid import bp as grid_bp

    app.register_blueprint(grid_bp)
    return app
