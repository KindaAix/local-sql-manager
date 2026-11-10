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
import unittest
from unittest import mock

from local_sql_manager.desktop.launcher import mcp_config_text
from local_sql_manager.mcp_server import build_mcp
from local_sql_manager.services import audit, schema
from local_sql_manager.services.library import open_library
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


class McpConfigTextCase(unittest.TestCase):
    def test_config_json_is_pasteable(self):
        text = mcp_config_text(8766)
        data = json.loads(text)
        self.assertEqual(
            data["mcpServers"]["local-sql-manager"]["url"],
            "http://127.0.0.1:8766/mcp",
        )

    def test_config_requires_valid_port(self):
        self.assertIsNone(mcp_config_text(None))
        self.assertIsNone(mcp_config_text(0))
        self.assertIsNone(mcp_config_text("abc"))
        self.assertIsNone(mcp_config_text(True))


if __name__ == "__main__":
    unittest.main()
