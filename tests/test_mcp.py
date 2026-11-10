"""MCP 工具的测试：直接调用工具函数，不经 HTTP。

每个工具都用 library.path 新开短连接，因此这里可以同时持有 library.conn 做
准备与断言；写入结果一律用标准库 sqlite3 重新打开数据库核对，不信任工具
自己返回的结构。
"""

import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from local_sql_manager.desktop.launcher import mcp_config_text
from local_sql_manager.mcp_server import (
    TokenAuthMiddleware,
    build_mcp,
    make_http_app,
)
from local_sql_manager.services import audit, schema, views
from local_sql_manager.services.library import open_library
from local_sql_manager.services.models import ViewConfig
from local_sql_manager.services.rows import insert_rows
from local_sql_manager.services.schema import ColumnSpec


def as_functions(mcp) -> dict:
    """把已注册的工具取成 {名字: 普通函数}。"""
    return {tool.name: tool.fn for tool in asyncio.run(mcp.list_tools())}


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


class McpToolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "测试.sqlite")
        self.lib = open_library(self.path)
        self.addCleanup(self.lib.conn.close)

        schema.create_table(
            self.lib.conn,
            "订单",
            [
                ColumnSpec("状态", "select", ("新建", "完成")),
                ColumnSpec("金额", "number"),
                ColumnSpec("备注", "text"),
            ],
        )
        self.insert_op = insert_rows(
            self.lib.conn,
            "订单",
            [
                {"状态": "完成", "金额": 100, "备注": "首单"},
                {"状态": "新建", "金额": 50, "备注": None},
                {"状态": "新建", "金额": 30, "备注": "补单"},
            ],
            source="web",
        )
        self.lib.conn.commit()

        self.mcp = build_mcp(self.lib)
        self.tools = as_functions(self.mcp)

    def rows(self, table="订单", where="1=1"):
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                f"SELECT * FROM {_quote(table)} WHERE {where} ORDER BY \"id\""
            ).fetchall()
        finally:
            conn.close()

    def audit_rows(self, table="订单"):
        return audit.history(self.lib.conn, table)

    # --- 读工具 ---------------------------------------------------

    def test_lists_library_tables_and_schema(self):
        libraries = self.tools["list_libraries"]()
        self.assertEqual(len(libraries), 1)
        self.assertTrue(libraries[0]["current"])
        self.assertEqual(libraries[0]["path"], self.path)

        self.assertIn("订单", self.tools["list_tables"]())
        fields = self.tools["get_schema"](table="订单")
        self.assertEqual([item["name"] for item in fields], ["状态", "金额", "备注"])
        self.assertEqual(fields[0]["type"], "select")
        self.assertEqual(fields[0]["options"], ["新建", "完成"])

    def test_get_rows_paginates(self):
        page = self.tools["get_rows"](table="订单", page=2, page_size=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["page"], 2)
        self.assertEqual(page["page_size"], 2)
        self.assertEqual(len(page["rows"]), 1)
        self.assertEqual(page["rows"][0]["金额"], 30)
        self.assertEqual(page["groups"], [])

    def test_count_rows_applies_filters(self):
        total = self.tools["count_rows"](table="订单")
        self.assertEqual(total, 3)
        filtered = self.tools["count_rows"](
            table="订单", filters=[{"column": "金额", "op": "gt", "value": 50}]
        )
        self.assertEqual(filtered, 1)

    def test_list_views_returns_default_view(self):
        views = self.tools["list_views"](table="订单")
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["name"], "全部记录")
        self.assertTrue(views[0]["is_default"])
        self.assertEqual(views[0]["config"]["filters"], [])

    def test_unknown_table_is_readable_error(self):
        with self.assertRaises(ValueError) as caught:
            self.tools["get_rows"](table="不存在的表")
        self.assertIn("表不存在", str(caught.exception))

    # --- 写入与审计 -----------------------------------------------

    def test_update_rows_writes_mcp_audit(self):
        result = self.tools["update_rows"](
            table="订单", changes=[{"id": 1, "金额": 77}]
        )
        self.assertIsInstance(result["op"], int)

        latest = self.audit_rows()[0]
        self.assertEqual(latest["source"], "mcp")
        self.assertEqual(latest["kind"], "update")
        self.assertEqual(self.rows(where='"id" = 1')[0]["金额"], 77)

    def test_update_without_expected_updates_row(self):
        self.tools["update_rows"](table="订单", changes=[{"id": 2, "备注": "改过"}])
        self.assertEqual(self.rows(where='"id" = 2')[0]["备注"], "改过")

    def test_stale_expected_raises_conflict(self):
        stale = self.rows(where='"id" = 1')[0]["updated_at"]
        # updated_at 只精确到秒，同一秒内的两次工具写入无法区分版本；
        # 这里直接用另一条连接模拟别处的写入，制造真正的版本差。
        other = sqlite3.connect(self.path)
        try:
            other.execute(
                'UPDATE "订单" SET "金额" = 10, "updated_at" = ? WHERE "id" = 1',
                ("2099-01-01T00:00:00Z",),
            )
            other.commit()
        finally:
            other.close()

        with self.assertRaises(ValueError) as caught:
            self.tools["update_rows"](
                table="订单",
                changes=[{"id": 1, "金额": 20}],
                expected={"1": stale},
            )
        self.assertIn("已被修改", str(caught.exception))
        self.assertEqual(self.rows(where='"id" = 1')[0]["金额"], 10)

    def test_create_rows_returns_ids_and_audits(self):
        result = self.tools["create_rows"](
            table="订单",
            rows=[{"状态": "完成", "金额": 5, "备注": "agent 新增"}],
        )
        self.assertEqual(len(result["ids"]), 1)
        self.assertEqual(self.audit_rows()[0]["source"], "mcp")
        self.assertEqual(self.audit_rows()[0]["kind"], "insert")
        self.assertEqual(self.tools["count_rows"](table="订单"), 4)

    def test_delete_and_restore_are_soft(self):
        deleted = self.tools["delete_rows"](table="订单", ids=[1])
        self.assertIsInstance(deleted["op"], int)
        self.assertEqual(self.tools["count_rows"](table="订单"), 2)
        # 行还在物理表里，只是 deleted_at 有值。
        self.assertIsNotNone(self.rows(where='"id" = 1')[0]["deleted_at"])

        self.tools["restore_rows"](table="订单", ids=[1])
        self.assertEqual(self.tools["count_rows"](table="订单"), 3)

    def test_undo_restores_previous_operation(self):
        created = self.tools["create_rows"](
            table="订单", rows=[{"金额": 1, "备注": "待撤销"}]
        )
        undone = self.tools["undo"](op_id=created["op"])
        self.assertIsInstance(undone["op"], int)
        self.assertEqual(self.tools["count_rows"](table="订单"), 3)
        self.assertEqual(self.audit_rows()[0]["kind"], "undo")

    def test_create_table_add_field_and_merge(self):
        self.assertEqual(
            self.tools["create_table"](
                table="客户", columns=[{"name": "姓名", "type": "text"}]
            ),
            "客户",
        )
        added = self.tools["add_field"](table="客户", name="电话", type="text")
        self.assertIsInstance(added["op"], int)
        self.assertEqual(
            [item["name"] for item in self.tools["get_schema"](table="客户")],
            ["姓名", "电话"],
        )

        self.tools["create_table"](
            table="客户二", columns=[{"name": "姓名", "type": "text"}]
        )
        self.tools["create_rows"](table="客户二", rows=[{"姓名": "张三"}])
        merged = self.tools["merge_tables"](
            tables=["客户", "客户二"], target="客户合并"
        )
        self.assertIsInstance(merged["op"], int)
        self.assertIn("客户合并", self.tools["list_tables"]())
        page = self.tools["get_rows"](table="客户合并")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["rows"][0]["姓名"], "张三")

    def test_history_can_filter_by_table(self):
        self.tools["update_rows"](table="订单", changes=[{"id": 3, "金额": 9}])
        records = self.tools["get_history"](table="订单", limit=5)
        self.assertTrue(records)
        self.assertTrue(all(item["table"] == "订单" for item in records))
        self.assertEqual(records[0]["source"], "mcp")

    # --- 默认视图与筛选校验 ---------------------------------------

    def test_get_rows_and_count_rows_use_default_view_filters(self):
        view_id = views.create_view(
            self.lib.conn,
            "订单",
            "只看完成",
            ViewConfig(
                filters=[{"column": "状态", "op": "eq", "value": "完成"}],
            ),
        )
        self.lib.conn.commit()
        self.assertIsInstance(view_id, int)

        page = self.tools["get_rows"](table="订单")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["rows"][0]["状态"], "完成")
        self.assertEqual(self.tools["count_rows"](table="订单"), 1)
        # 显式筛选叠加在默认视图之上，而不是替换掉它。
        self.assertEqual(
            self.tools["count_rows"](
                table="订单", filters=[{"column": "金额", "op": "gt", "value": 60}]
            ),
            1,
        )
        self.assertEqual(
            self.tools["count_rows"](
                table="订单", filters=[{"column": "金额", "op": "gt", "value": 200}]
            ),
            0,
        )

    def test_count_rows_rejects_invalid_filters(self):
        cases = [
            ("未知列", [{"column": "不存在", "op": "eq", "value": 1}]),
            ("错误操作符", [{"column": "金额", "op": "contains", "value": 1}]),
            ("非对象", ["金额 > 1"]),
        ]
        for label, filters in cases:
            with self.subTest(label=label):
                with self.assertRaises(ValueError) as caught:
                    self.tools["count_rows"](table="订单", filters=filters)
                self.assertIn("筛选条件不支持", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.tools["count_rows"](table="订单", filters={"column": "金额"})
        self.assertIn("filters", str(caught.exception))

    # --- 表与行参数的边界 -----------------------------------------

    def test_internal_tables_are_not_readable(self):
        for table in ("__lsm_op", "__lsm_meta"):
            with self.subTest(table=table):
                with self.assertRaises(ValueError) as caught:
                    self.tools["get_rows"](table=table)
                self.assertIn("不能操作内部表", str(caught.exception))
                with self.assertRaises(ValueError):
                    self.tools["count_rows"](table=table)

    def test_create_table_rejects_empty_and_taken_names(self):
        for name in ("", "   ", None):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    self.tools["create_table"](table=name, columns=[{"name": "a"}])
                self.assertIn("表名不能为空", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.tools["create_table"](table="订单", columns=[{"name": "a"}])
        self.assertEqual(str(caught.exception), "表已存在：订单")
        self.assertNotIn("already exists", str(caught.exception).lower())

    def test_update_rejects_soft_deleted_rows(self):
        self.tools["delete_rows"](table="订单", ids=[1])
        stale = self.rows(where='"id" = 1')[0]["updated_at"]
        for expected in (None, {"1": stale}):
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError) as caught:
                    self.tools["update_rows"](
                        table="订单",
                        changes=[{"id": 1, "金额": 1}],
                        expected=expected,
                    )
                self.assertEqual(str(caught.exception), "该行已删除，请先恢复")
        self.tools["restore_rows"](table="订单", ids=[1])
        self.tools["update_rows"](table="订单", changes=[{"id": 1, "金额": 1}])
        self.assertEqual(self.rows(where='"id" = 1')[0]["金额"], 1)

    def test_row_id_arguments_must_be_lists(self):
        for name in ("delete_rows", "restore_rows"):
            for value in ("1", None, 1, {"id": 1}):
                with self.subTest(tool=name, value=value):
                    with self.assertRaises(ValueError) as caught:
                        self.tools[name](table="订单", ids=value)
                    self.assertIn("ids", str(caught.exception))

    def test_history_of_dropped_table_still_readable(self):
        self.tools["update_rows"](table="订单", changes=[{"id": 3, "金额": 9}])
        schema.drop_table(self.lib.conn, "订单")
        self.lib.conn.commit()

        records = self.tools["get_history"](table="订单", limit=5)
        self.assertTrue(records)
        with self.assertRaises(ValueError) as caught:
            self.tools["get_rows"](table="订单")
        self.assertIn("表不存在", str(caught.exception))

    # --- 并发与错误映射 -------------------------------------------

    def test_busy_database_maps_to_chinese_message(self):
        blocker = sqlite3.connect(self.path, timeout=5)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute('UPDATE "订单" SET "金额" = 1 WHERE "id" = 1')
        try:
            with mock.patch("local_sql_manager.mcp_server.CONNECT_TIMEOUT", 0.1):
                with self.assertRaises(ValueError) as caught:
                    self.tools["create_rows"](table="订单", rows=[{"金额": 2}])
            self.assertEqual(str(caught.exception), "数据库正忙，请稍后重试")
            self.assertNotIn("locked", str(caught.exception))
        finally:
            blocker.rollback()

    def test_write_waits_for_external_writer(self):
        # A 开写事务不提交；B 的写工具应等锁而不是立刻 database is locked。
        blocker = sqlite3.connect(self.path, timeout=5)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute('UPDATE "订单" SET "金额" = 1 WHERE "id" = 1')

        outcome = {}

        def write():
            try:
                outcome["op"] = self.tools["update_rows"](
                    table="订单", changes=[{"id": 2, "金额": 42}]
                )
            except BaseException as exc:  # noqa: BLE001 - 原样带回主线程断言
                outcome["error"] = exc

        thread = threading.Thread(target=write)
        started = time.monotonic()
        thread.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive(), "写工具应当等锁而不是立刻失败")
        blocker.commit()
        thread.join(timeout=10)
        elapsed = time.monotonic() - started

        self.assertNotIn("error", outcome, repr(outcome.get("error")))
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertEqual(self.rows(where='"id" = 2')[0]["金额"], 42)

    def test_external_writer_waits_for_mcp_write(self):
        entered = threading.Event()
        release = threading.Event()
        real_record = audit.record

        def slow_record(*args, **kwargs):
            # 审计在 _write_txn 的 BEGIN IMMEDIATE 之后写，此时写锁已拿到。
            entered.set()
            release.wait(5)
            return real_record(*args, **kwargs)

        outcome = {}

        def mcp_write():
            try:
                outcome["op"] = self.tools["update_rows"](
                    table="订单", changes=[{"id": 1, "金额": 11}]
                )
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc

        wrote = {}

        def direct_write():
            # sqlite 连接不能跨线程使用，所以在写线程里自己开一条。
            direct = sqlite3.connect(self.path, timeout=5)
            try:
                direct.execute("BEGIN IMMEDIATE")
                direct.execute('UPDATE "订单" SET "金额" = 22 WHERE "id" = 2')
                direct.commit()
                wrote["done"] = True
            finally:
                direct.close()

        with mock.patch.object(audit, "record", side_effect=slow_record):
            thread = threading.Thread(target=mcp_write, daemon=True)
            writer = threading.Thread(target=direct_write, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(5), "写工具应当已经进入写事务")
                writer.start()
                time.sleep(0.3)
                self.assertTrue(writer.is_alive(), "另一条连接应等 MCP 的写事务")
            finally:
                release.set()
            thread.join(timeout=10)
            writer.join(timeout=10)

        self.assertNotIn("error", outcome, repr(outcome.get("error")))
        self.assertFalse(thread.is_alive())
        self.assertTrue(wrote.get("done"))
        self.assertEqual(self.rows(where='"id" = 1')[0]["金额"], 11)
        self.assertEqual(self.rows(where='"id" = 2')[0]["金额"], 22)

    # --- 只读模式 -------------------------------------------------

    def test_read_only_blocks_writes(self):
        read_only = as_functions(build_mcp(self.lib, read_only=True))
        for name, args in (
            ("create_rows", {"table": "订单", "rows": [{"金额": 1}]}),
            ("update_rows", {"table": "订单", "changes": [{"id": 1, "金额": 1}]}),
            ("delete_rows", {"table": "订单", "ids": [1]}),
            ("restore_rows", {"table": "订单", "ids": [1]}),
            ("create_table", {"table": "新表", "columns": [{"name": "a"}]}),
            ("add_field", {"table": "订单", "name": "新列", "type": "text"}),
            ("merge_tables", {"tables": ["订单", "订单"], "target": "合并"}),
            ("undo", {"op_id": self.insert_op}),
        ):
            with self.subTest(tool=name):
                with self.assertRaises(PermissionError) as caught:
                    read_only[name](**args)
                self.assertIn("只读", str(caught.exception))

        # 读工具在只读模式下照常可用。
        self.assertEqual(read_only["count_rows"](table="订单"), 3)

    # --- 连接管理 -------------------------------------------------

    def test_build_mcp_keeps_token_for_http_app(self):
        mcp = build_mcp(self.lib, token="tok")
        self.assertEqual(mcp.lsm_token, "tok")
        self.assertIsInstance(make_http_app(mcp), TokenAuthMiddleware)
        self.assertIsNotNone(make_http_app(build_mcp(self.lib)))

    def test_tool_calls_close_their_connections(self):
        opened = []
        real_connect = sqlite3.connect

        def track(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        with mock.patch("sqlite3.connect", side_effect=track):
            self.tools["get_rows"](table="订单")
            self.tools["update_rows"](table="订单", changes=[{"id": 1, "金额": 8}])

        self.assertTrue(opened)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


class TokenAuthCase(unittest.TestCase):
    TOKEN = "test-token-123"

    def _call(self, app, headers=None, query=b""):
        events = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            events.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": query,
            "headers": headers or [],
            "server": ("127.0.0.1", 8766),
            "client": ("127.0.0.1", 50000),
        }
        asyncio.run(app(scope, receive, send))
        return events

    def test_missing_or_wrong_token_is_rejected(self):
        for headers, query in (
            (None, b""),
            ([(b"authorization", b"Bearer wrong")], b""),
            ([(b"authorization", b"Basic abc")], b""),
            (None, b"token=wrong"),
        ):
            with self.subTest(headers=headers, query=query):
                inner = _Recorder()
                events = self._call(
                    TokenAuthMiddleware(inner, self.TOKEN), headers, query
                )
                self.assertEqual(events[0]["status"], 401)
                self.assertEqual(
                    events[1]["body"].decode("utf-8"), "未授权的 MCP 请求"
                )
                self.assertFalse(inner.called)

    def test_bearer_token_passes(self):
        inner = _Recorder()
        events = self._call(
            TokenAuthMiddleware(inner, self.TOKEN),
            [(b"authorization", b"bearer test-token-123")],
        )
        self.assertTrue(inner.called)
        self.assertEqual(events[0]["status"], 200)

    def test_query_token_passes(self):
        inner = _Recorder()
        events = self._call(
            TokenAuthMiddleware(inner, self.TOKEN), None, b"token=test-token-123"
        )
        self.assertTrue(inner.called)
        self.assertEqual(events[0]["status"], 200)

    def test_lifespan_scope_passes_through(self):
        seen = []

        async def app(scope, receive, send):
            seen.append(scope["type"])

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            pass

        asyncio.run(
            TokenAuthMiddleware(app, self.TOKEN)(
                {"type": "lifespan"}, receive, send
            )
        )
        self.assertEqual(seen, ["lifespan"])

    def test_make_http_app_wraps_only_when_token_present(self):
        plain = make_http_app(_FakeMCP(None))
        self.assertIsInstance(plain, _Recorder)
        wrapped = make_http_app(_FakeMCP("tok"))
        self.assertIsInstance(wrapped, TokenAuthMiddleware)
        self.assertEqual(wrapped.token, "tok")


class _Recorder:
    """记录是否被调用的最小 ASGI 应用。"""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _FakeMCP:
    def __init__(self, token):
        self.lsm_token = token

    def http_app(self, path="/mcp"):
        return _Recorder()


class McpConfigTextCase(unittest.TestCase):
    def test_config_json_is_pasteable(self):
        text = mcp_config_text(8766)
        data = json.loads(text)
        self.assertEqual(
            data["mcpServers"]["local-sql-manager"]["url"],
            "http://127.0.0.1:8766/mcp",
        )

    def test_config_json_carries_authorization_header(self):
        data = json.loads(mcp_config_text(8766, "tok-1"))
        server = data["mcpServers"]["local-sql-manager"]
        self.assertEqual(server["headers"], {"Authorization": "Bearer tok-1"})
        # 没给 token 时不输出空 headers。
        plain = json.loads(mcp_config_text(8766))
        self.assertNotIn("headers", plain["mcpServers"]["local-sql-manager"])

    def test_config_requires_valid_port(self):
        self.assertIsNone(mcp_config_text(None))
        self.assertIsNone(mcp_config_text(0))
        self.assertIsNone(mcp_config_text("abc"))
        self.assertIsNone(mcp_config_text(True))


if __name__ == "__main__":
    unittest.main()
