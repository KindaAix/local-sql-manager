"""Flask 应用工厂。"""

from __future__ import annotations

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")

    @app.get("/healthz")
    def healthz() -> str:
        return "ok"

    return app
