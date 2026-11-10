"""桌面启动、配置、日志与托盘的测试。"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from local_sql_manager.desktop import launcher
from local_sql_manager.desktop.config import (
    config_path,
    default_libraries_dir,
    load_config,
)
from local_sql_manager.desktop.launcher import (
    acquire_single_instance,
    pick_port,
    release_single_instance,
)
from local_sql_manager.desktop.logging_setup import setup_logging
from local_sql_manager.desktop.tray import TRAY_MENU_LABELS


class TestSingleInstance(unittest.TestCase):
    def test_second_instance_refused(self):
        name = f"lsm-test-{uuid4()}"
        self.assertTrue(acquire_single_instance(name))
        try:
            self.assertFalse(acquire_single_instance(name))
        finally:
            release_single_instance(name)

    def test_release_allows_reacquire(self):
        name = f"lsm-test-{uuid4()}"
        self.assertTrue(acquire_single_instance(name))
        release_single_instance(name)
        self.assertTrue(acquire_single_instance(name))
        release_single_instance(name)

    def test_main_refused_when_already_running(self):
        name = f"lsm-test-{uuid4()}"
        self.assertTrue(acquire_single_instance(name))
        try:
            with mock.patch.object(launcher, "APP_MUTEX", name):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(launcher.main(), 1)
        finally:
            release_single_instance(name)


class TestPickPort(unittest.TestCase):
    def test_pick_port_skips_busy(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            busy = s.getsockname()[1]
            self.assertNotEqual(pick_port(busy), busy)

    def test_pick_port_keeps_free(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
        self.assertEqual(pick_port(free), free)

    def test_pick_port_zero_returns_usable_port(self):
        port = pick_port(0)
        self.assertGreater(port, 0)
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old = os.environ.get("LSM_DATA_DIR")
        os.environ["LSM_DATA_DIR"] = str(self.root)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LSM_DATA_DIR", None)
        else:
            os.environ["LSM_DATA_DIR"] = self._old
        self._tmp.cleanup()

    def test_defaults(self):
        cfg = load_config()
        self.assertEqual(cfg["port"], 8765)
        self.assertEqual(cfg["mcp_port"], 8766)
        self.assertFalse(cfg["mcp_read_only"])
        self.assertEqual(cfg["libraries_dir"], str(default_libraries_dir()))
        self.assertEqual(config_path(), self.root / "config.json")

    def test_reads_existing_values_and_ignores_unknown(self):
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(
            json.dumps({"port": 9000, "mcp_read_only": True, "unknown": 1}),
            encoding="utf-8",
        )
        cfg = load_config()
        self.assertEqual(cfg["port"], 9000)
        self.assertTrue(cfg["mcp_read_only"])
        self.assertNotIn("unknown", cfg)

    def test_broken_json_falls_back_to_defaults(self):
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(load_config()["port"], 8765)


class TestLogging(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if getattr(handler, "_lsm", False):
                root_logger.removeHandler(handler)
                handler.close()
        self._tmp.cleanup()

    def _lsm_handlers(self):
        return [h for h in logging.getLogger().handlers if getattr(h, "_lsm", False)]

    def test_writes_daily_log_file(self):
        setup_logging(self.root)
        logging.getLogger("local_sql_manager.test").info("你好")
        files = list(self.root.glob("app-*.log"))
        self.assertEqual(len(files), 1)
        self.assertIn("你好", files[0].read_text(encoding="utf-8"))

    def test_setup_is_idempotent(self):
        setup_logging(self.root)
        setup_logging(self.root)
        self.assertEqual(len(self._lsm_handlers()), 1)


class TestTray(unittest.TestCase):
    def test_menu_has_four_actions_plus_quit(self):
        self.assertEqual(
            TRAY_MENU_LABELS,
            ("打开界面", "复制 MCP 配置", "打开数据目录", "打开日志", "退出"),
        )

class TestMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._old = os.environ.get("LSM_DATA_DIR")
        os.environ["LSM_DATA_DIR"] = str(self.root)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LSM_DATA_DIR", None)
        else:
            os.environ["LSM_DATA_DIR"] = self._old
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if getattr(handler, "_lsm", False):
                root_logger.removeHandler(handler)
                handler.close()
        self._tmp.cleanup()

    def test_starts_server_and_cleans_up(self):
        db = self.root / "lib.sqlite"
        (self.root / "config.json").write_text(
            json.dumps({"last_library": str(db), "port": 0}), encoding="utf-8"
        )
        name = f"lsm-test-{uuid4()}"
        with mock.patch.object(launcher, "APP_MUTEX", name), mock.patch.object(
            launcher, "run_tray"
        ) as run_tray, mock.patch.object(launcher, "open_browser") as open_browser:
            self.assertEqual(launcher.main(), 0)
        self.assertTrue(db.exists())
        run_tray.assert_called_once()
        open_browser.assert_called_once()
        # 退出后单实例锁已释放，可以再次获取。
        self.assertTrue(acquire_single_instance(name))
        release_single_instance(name)


if __name__ == "__main__":
    unittest.main()