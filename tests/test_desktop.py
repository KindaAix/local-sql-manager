"""桌面启动、配置、日志与托盘的测试。"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4

from local_sql_manager.desktop import launcher, tray
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


class _FakeMenuItem:
    def __init__(self, text, action=None, **kwargs):
        self.text = text
        self.action = action
        self.enabled = kwargs.get("enabled", True)
        self.default = kwargs.get("default", False)


class _FakeMenu(list):
    def __init__(self, *items):
        super().__init__(items)


class _FakeIcon:
    def __init__(self, name, image, title, menu):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.stopped = False

    def stop(self):
        self.stopped = True


def _fake_pystray():
    return types.SimpleNamespace(
        Menu=_FakeMenu, MenuItem=_FakeMenuItem, Icon=_FakeIcon
    )


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
            with mock.patch.object(launcher, "APP_MUTEX", name), mock.patch.object(
                launcher, "_show_error"
            ) as show_error:
                self.assertEqual(launcher.main(), 1)
            show_error.assert_called_once()
            message = show_error.call_args.args[0]
            self.assertIn("已在运行", message)
            self.assertIn("http://127.0.0.1:", message)
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

    def test_pick_port_clamps_out_of_range(self):
        for preferred in (70000, -5, "abc", None):
            with self.subTest(preferred=preferred):
                port = pick_port(preferred)
                self.assertGreater(port, 0)
                self.assertLessEqual(port, 65535)


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

    def _write(self, payload):
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps(payload), encoding="utf-8")

    def test_defaults(self):
        cfg = load_config()
        self.assertEqual(cfg["port"], 8765)
        self.assertEqual(cfg["mcp_port"], 8766)
        self.assertFalse(cfg["mcp_read_only"])
        self.assertEqual(cfg["libraries_dir"], str(default_libraries_dir()))
        self.assertEqual(config_path(), self.root / "config.json")

    def test_reads_existing_values_and_ignores_unknown(self):
        self._write({"port": 9000, "mcp_read_only": True, "unknown": 1})
        cfg = load_config()
        self.assertEqual(cfg["port"], 9000)
        self.assertTrue(cfg["mcp_read_only"])
        self.assertNotIn("unknown", cfg)

    def test_broken_json_falls_back_to_defaults(self):
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(load_config()["port"], 8765)

    def test_out_of_range_ports_fall_back(self):
        self._write({"port": 70000, "mcp_port": -5})
        cfg = load_config()
        self.assertEqual(cfg["port"], 8765)
        self.assertEqual(cfg["mcp_port"], 8766)

    def test_generates_and_persists_mcp_token(self):
        cfg = load_config()
        token = cfg["mcp_token"]
        self.assertGreaterEqual(len(token), 20)
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        self.assertEqual(saved["mcp_token"], token)
        # 第二次读取沿用同一个 token，粘贴到 MCP 客户端的配置不会失效。
        self.assertEqual(load_config()["mcp_token"], token)

    def test_existing_config_without_token_gets_one(self):
        self._write({"port": 9000})
        cfg = load_config()
        self.assertTrue(cfg["mcp_token"])
        saved = json.loads(config_path().read_text(encoding="utf-8"))
        self.assertEqual(saved["mcp_token"], cfg["mcp_token"])
        self.assertEqual(saved["port"], 9000)
        self.assertEqual(load_config()["mcp_token"], cfg["mcp_token"])

    def test_existing_token_is_kept(self):
        self._write({"mcp_token": "keep-me"})
        self.assertEqual(load_config()["mcp_token"], "keep-me")

    def test_string_booleans_are_explicit(self):
        self._write({"mcp_read_only": "false"})
        self.assertFalse(load_config()["mcp_read_only"])
        self._write({"mcp_read_only": "yes"})
        self.assertTrue(load_config()["mcp_read_only"])


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

    def test_purges_logs_older_than_14_days(self):
        now = datetime.now(timezone.utc)
        old_far = (now - timedelta(days=40)).strftime("%Y-%m-%d")
        old_edge = (now - timedelta(days=20)).strftime("%Y-%m-%d")
        recent = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        for day in (old_far, old_edge, recent):
            (self.root / f"app-{day}.log").write_text("旧日志", encoding="utf-8")

        setup_logging(self.root)
        logging.getLogger("local_sql_manager.test").info("触发清理")

        self.assertFalse((self.root / f"app-{old_far}.log").exists())
        self.assertFalse((self.root / f"app-{old_edge}.log").exists())
        self.assertTrue((self.root / f"app-{recent}.log").exists())


class TestTray(unittest.TestCase):
    def test_menu_has_four_actions_plus_quit(self):
        self.assertEqual(
            TRAY_MENU_LABELS,
            ("打开界面", "复制 MCP 配置", "打开数据目录", "打开日志", "退出"),
        )

    def _build(self, mcp_config):
        with mock.patch.dict(sys.modules, {"pystray": _fake_pystray()}), mock.patch.object(
            tray, "build_icon_image", return_value=object()
        ):
            return tray.create_icon(
                url="http://127.0.0.1:8765/",
                data_dir="C:/data",
                log_dir="C:/logs",
                on_quit=lambda: None,
                mcp_config=mcp_config,
            )

    def test_menu_has_five_items_and_mcp_disabled_without_server(self):
        icon = self._build(None)
        items = list(icon.menu)
        self.assertEqual(len(items), 5)
        self.assertEqual([item.text for item in items], list(TRAY_MENU_LABELS))
        self.assertFalse(items[1].enabled)
        self.assertTrue(items[0].default)

    def test_mcp_item_enabled_when_server_connected(self):
        icon = self._build(lambda: "{}")
        self.assertTrue(icon.menu[1].enabled)


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

    def _write_config(self, payload):
        (self.root / "config.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_starts_server_and_cleans_up(self):
        db = self.root / "lib.sqlite"
        self._write_config({"last_library": str(db), "port": 0})
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

    def test_broken_library_reports_error(self):
        bad = self.root / "bad.sqlite"
        bad.write_bytes(b"not a sqlite database")
        self._write_config({"last_library": str(bad), "port": 70000})
        name = f"lsm-test-{uuid4()}"
        with mock.patch.object(launcher, "APP_MUTEX", name), mock.patch.object(
            launcher, "_show_error"
        ) as show_error, mock.patch.object(launcher, "run_tray") as run_tray:
            self.assertEqual(launcher.main(), 1)
        show_error.assert_called_once()
        run_tray.assert_not_called()
        self.assertTrue(acquire_single_instance(name))
        release_single_instance(name)

    def test_invalid_port_falls_back_and_starts(self):
        db = self.root / "lib.sqlite"
        self._write_config({"last_library": str(db), "port": 70000})
        name = f"lsm-test-{uuid4()}"
        with mock.patch.object(launcher, "APP_MUTEX", name), mock.patch.object(
            launcher, "run_tray"
        ) as run_tray, mock.patch.object(launcher, "open_browser"):
            self.assertEqual(launcher.main(), 0)
        run_tray.assert_called_once()
        self.assertTrue(db.exists())


if __name__ == "__main__":
    unittest.main()
