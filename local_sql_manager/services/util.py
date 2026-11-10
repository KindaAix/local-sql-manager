"""通用时间工具。"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow_iso() -> str:
    """返回 UTC 的 ISO8601 时间，精确到秒，例如 2026-11-10T08:00:00Z。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
