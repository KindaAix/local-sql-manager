"""运行日志：按天写入 app-YYYY-MM-DD.log，保留 14 天。"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import log_dir as default_log_dir

KEEP_DAYS = 14


class DailyFileHandler(logging.Handler):
    """按 UTC 日期切换文件，并清理超过保留期的旧日志。"""

    def __init__(self, directory: Path, keep_days: int = KEEP_DAYS) -> None:
        super().__init__()
        self.directory = Path(directory)
        self.keep_days = keep_days
        self._day: str | None = None
        self._stream = None

    def _open_for_today(self) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day == self._day and self._stream is not None:
            return
        self._close_stream()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stream = (self.directory / f"app-{day}.log").open(
            "a", encoding="utf-8"
        )
        self._day = day
        self._purge()

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def _purge(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.keep_days)
        for path in self.directory.glob("app-*.log"):
            try:
                day = datetime.strptime(path.stem[4:], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue
            if day < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._open_for_today()
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self._close_stream()
        super().close()


def setup_logging(directory: str | Path | None = None) -> None:
    """安装进程级日志；重复调用不会重复挂 handler。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_lsm", False):
            root.removeHandler(handler)
            handler.close()

    handler = DailyFileHandler(Path(directory) if directory else default_log_dir())
    handler._lsm = True  # type: ignore[attr-defined]
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
