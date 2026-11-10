"""网格页面的渲染与路由测试。"""

import os
import tempfile
import unittest
from urllib.parse import unquote

from local_sql_manager.app import create_app
from local_sql_manager.services import rows, schema, views
from local_sql_manager.services.library import open_library
from local_sql_manager.services.schema import ColumnSpec


class GridPageTest(unittest.TestCase):
    """一张受管的「订单」表，三条数据，其中一条之后软删。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.library = open_library(os.path.join(tmp.name, "grid.sqlite"))
        self.addCleanup(self.library.conn.close)
        self.conn = self.library.conn
        schema.create_table(
            self.conn,
            "订单",
            [
                ColumnSpec("单号", "text"),
                ColumnSpec("金额", "number"),
                ColumnSpec("状态", "select"),
            ],
        )
        rows.insert_rows(
            self.conn,
            "订单",
            [
                {"单号": "SO-2026-0148", "金额": 520, "状态": "已发货"},
                {"单号": "SO-2026-0149", "金额": 80, "状态": "已发货"},
                {"单号": "SO-2026-0150", "金额": 100, "状态": "待付款"},
            ],
        )
        self.conn.commit()
        self.app = create_app(self.library)
        self.client = self.app.test_client()

    def html(self, path: str) -> str:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_root_redirects_to_first_table(self):
        schema.create_table(self.conn, "客户", [ColumnSpec("姓名", "text")])
        self.conn.commit()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(unquote(response.headers["Location"]), "/t/订单/")

    def test_grid_renders_rows_and_sidebar(self):
        html = self.html("/t/订单/")
        self.assertIn("订单", html)
        self.assertIn("全部记录", html)
        self.assertIn("SO-2026-0148", html)
        self.assertNotIn("badge-readonly", html)

    def test_soft_deleted_rows_are_hidden(self):
        op = rows.soft_delete(self.conn, "订单", [1])
        self.assertIsNotNone(op)
        self.conn.commit()
        html = self.html("/t/订单/")
        self.assertNotIn("SO-2026-0148", html)
        self.assertIn("SO-2026-0149", html)

    def test_pagination_splits_rows(self):
        extra = [
            {"单号": f"SO-2026-{index:04d}", "金额": index, "状态": "已发货"}
            for index in range(151, 208)
        ]
        rows.insert_rows(self.conn, "订单", extra)
        self.conn.commit()

        first = self.html("/t/订单/?size=50")
        second = self.html("/t/订单/?size=50&page=2")

        self.assertEqual(first.count('class="grid-row"'), 50)
        self.assertEqual(second.count('class="grid-row"'), 10)
        self.assertIn("SO-2026-0148", first)
        self.assertNotIn("SO-2026-0148", second)
        self.assertIn("共 60 行", first)
        self.assertIn("共 60 行", second)

    def test_view_filter_limits_rows(self):
        view_id = views.create_view(
            self.conn,
            "订单",
            "只看待付款",
            {"filters": [{"column": "状态", "op": "eq", "value": "待付款"}]},
        )
        self.conn.commit()
        html = self.html(f"/t/订单/?view={view_id}")
        self.assertIn("只看待付款", html)
        self.assertIn("SO-2026-0150", html)
        self.assertNotIn("SO-2026-0148", html)

    def test_bad_view_falls_back_to_default(self):
        schema.create_table(self.conn, "客户", [ColumnSpec("姓名", "text")])
        foreign = views.create_view(
            self.conn,
            "客户",
            "客户视图",
            {"filters": [{"column": "姓名", "op": "eq", "value": "张三"}]},
        )
        self.conn.commit()
        for query in ("view=9999", "view=abc", f"view={foreign}"):
            html = self.html(f"/t/订单/?{query}")
            self.assertIn("全部记录", html)
            self.assertIn("SO-2026-0148", html)

    def test_duplicate_view_falls_back(self):
        # 同一张表有两个视图，view 参数缺失时用默认（第一个）视图。
        first = views.create_view(self.conn, "订单", "默认视图", {})
        second = views.create_view(
            self.conn,
            "订单",
            "待付款",
            {"filters": [{"column": "状态", "op": "eq", "value": "待付款"}]},
        )
        self.conn.commit()
        self.assertNotEqual(first, second)
        html = self.html("/t/订单/")
        self.assertIn("SO-2026-0148", html)

    def test_missing_or_internal_table_returns_404(self):
        self.assertEqual(self.client.get("/t/没有这张表/").status_code, 404)
        self.assertEqual(self.client.get("/t/__lsm_meta/").status_code, 404)

    def test_group_view_shows_counts_and_aggregates(self):
        view_id = views.create_view(
            self.conn,
            "订单",
            "按状态分组",
            {
                "group": {"column": "状态"},
                "aggregates": [{"column": "金额", "fn": "sum"}],
            },
        )
        self.conn.commit()
        html = self.html(f"/t/订单/?view={view_id}")
        self.assertIn("共 2 条", html)
        self.assertIn("600", html)
        self.assertNotIn("（续）", html)

    def test_group_header_marks_continuation(self):
        schema.create_table(self.conn, "分组表", [ColumnSpec("类别", "text")])
        payload = [{"类别": "A"} for _ in range(60)] + [{"类别": "B"} for _ in range(5)]
        rows.insert_rows(self.conn, "分组表", payload)
        view_id = views.create_view(
            self.conn, "分组表", "按类别", {"group": {"column": "类别"}}
        )
        self.conn.commit()

        first = self.html(f"/t/分组表/?view={view_id}&size=50")
        second = self.html(f"/t/分组表/?view={view_id}&size=50&page=2")
        self.assertIn("共 60 条", first)
        self.assertNotIn("（续）", first)
        self.assertIn("（续）", second)

    def test_unmanaged_table_is_read_only(self):
        self.conn.execute('CREATE TABLE "外部表" ("编号" TEXT, "说明" TEXT)')
        self.conn.execute('INSERT INTO "外部表" ("编号", "说明") VALUES (?, ?)', ("X-1", "外部数据"))
        self.conn.commit()

        html = self.html("/t/外部表/")
        self.assertIn("只读", html)
        self.assertIn("X-1", html)
        self.assertIn("说明", html)

    def test_empty_table_shows_placeholder(self):
        schema.create_table(self.conn, "空表", [ColumnSpec("名称", "text")])
        self.conn.commit()
        html = self.html("/t/空表/")
        self.assertIn("还没有数据", html)

    def test_pager_links_keep_view_and_size(self):
        view_id = views.create_view(self.conn, "订单", "全部", {})
        self.conn.commit()
        rows.insert_rows(
            self.conn,
            "订单",
            [{"单号": f"SO-BULK-{index}", "金额": index, "状态": "已发货"} for index in range(60)],
        )
        self.conn.commit()
        html = self.html(f"/t/订单/?view={view_id}&size=50")
        self.assertIn(f"view={view_id}", html)
        self.assertIn("size=50", html)
        self.assertIn("size=200", html)


class EmptyLibraryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.library = open_library(os.path.join(tmp.name, "empty.sqlite"))
        self.addCleanup(self.library.conn.close)
        self.client = create_app(self.library).test_client()

    def test_empty_library_renders_placeholder(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("导入 Excel", html)
        self.assertIn("还没有数据表", html)
        self.assertIn('title="导入向导将在后续版本提供"', html)


if __name__ == "__main__":
    unittest.main()
