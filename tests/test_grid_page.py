"""网格页面的渲染与路由测试。"""

import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from urllib.parse import unquote

from local_sql_manager.app import create_app
from local_sql_manager.services import rows, schema, views
from local_sql_manager.services.library import open_library
from local_sql_manager.services.schema import ColumnSpec


class GridPageTest(unittest.TestCase):
    """一张受管的「订单」表，三条数据。"""

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
        self.assertIn('aria-current="page"', html)
        self.assertIn('scope="col"', html)

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

    def test_page_beyond_last_redirects_to_last_page(self):
        rows.insert_rows(
            self.conn,
            "订单",
            [{"单号": f"SO-BULK-{index}", "金额": index, "状态": "已发货"} for index in range(60)],
        )
        self.conn.commit()
        response = self.client.get("/t/订单/?size=50&page=9")
        self.assertEqual(response.status_code, 302)
        location = unquote(response.headers["Location"])
        self.assertIn("page=2", location)
        self.assertIn("size=50", location)

    def test_out_of_range_page_never_500(self):
        # 普通视图：page=999 夹取到最后一页。
        response = self.client.get("/t/订单/?page=999")
        self.assertEqual(response.status_code, 302)
        self.assertIn("page=1", unquote(response.headers["Location"]))

        # 分组视图：越界页也不得因为空组块抛 IndexError。
        view_id = views.create_view(
            self.conn, "订单", "按状态", {"group": {"column": "状态"}}
        )
        self.conn.commit()
        response = self.client.get(f"/t/订单/?view={view_id}&page=99")
        self.assertEqual(response.status_code, 302)
        self.assertIn("page=1", unquote(response.headers["Location"]))

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

    def test_missing_table_page_keeps_sidebar(self):
        response = self.client.get("/t/没有这张表/")
        self.assertEqual(response.status_code, 404)
        html = response.get_data(as_text=True)
        self.assertIn("找不到这张表", html)
        self.assertIn("sidebar", html)
        self.assertIn("订单", html)

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

    def test_group_continuation_uses_group_offset(self):
        # 50 行 A + 55 行 B：B 从第 2 页第一行开始，不该标（续）；第 3 页才续。
        schema.create_table(self.conn, "边界表", [ColumnSpec("类别", "text")])
        rows.insert_rows(
            self.conn,
            "边界表",
            [{"类别": "A"} for _ in range(50)] + [{"类别": "B"} for _ in range(55)],
        )
        view_id = views.create_view(
            self.conn, "边界表", "按类别", {"group": {"column": "类别"}}
        )
        self.conn.commit()

        first = self.html(f"/t/边界表/?view={view_id}&size=50")
        second = self.html(f"/t/边界表/?view={view_id}&size=50&page=2")
        third = self.html(f"/t/边界表/?view={view_id}&size=50&page=3")

        self.assertNotIn("（续）", first)
        self.assertNotIn("（续）", second)
        self.assertIn("共 55 条", second)
        self.assertIn("（续）", third)

    def test_group_label_marks_blank_values(self):
        # 写入层会把空串归一成 NULL，这里直接落库造出「空串」与「NULL」两组。
        schema.create_table(self.conn, "空值表", [ColumnSpec("类别", "text")])
        for value in ("", None, "有值"):
            self.conn.execute(
                'INSERT INTO "空值表" ("类别", "created_at", "updated_at")'
                " VALUES (?, ?, ?)",
                (value, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        view_id = views.create_view(
            self.conn, "空值表", "按类别", {"group": {"column": "类别"}}
        )
        self.conn.commit()
        html = self.html(f"/t/空值表/?view={view_id}")
        self.assertGreaterEqual(html.count("（空）"), 2)

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
        self.assertIn("暂无数据", html)

    def test_pager_links_carry_view_page_and_size(self):
        view_id = views.create_view(self.conn, "订单", "全部", {})
        rows.insert_rows(
            self.conn,
            "订单",
            [{"单号": f"SO-BULK-{index}", "金额": index, "状态": "已发货"} for index in range(60)],
        )
        self.conn.commit()

        html = self.html(f"/t/订单/?view={view_id}&size=50")
        next_links = re.findall(r'<a class="pager-link" href="([^"]+)">下一页</a>', html)
        self.assertEqual(len(next_links), 1)
        href = next_links[0].replace("&amp;", "&")
        self.assertIn("page=2", href)
        self.assertIn(f"view={view_id}", href)
        self.assertIn("size=50", href)
        # page=1 时上一页只是禁用文本，不应有链接。
        self.assertNotIn(">上一页</a>", html)
        self.assertIn(">上一页</span>", html)

        second = self.html(f"/t/订单/?view={view_id}&size=50&page=2")
        prev_links = re.findall(r'<a class="pager-link" href="([^"]+)">上一页</a>', second)
        self.assertEqual(len(prev_links), 1)
        prev = prev_links[0].replace("&amp;", "&")
        self.assertIn("page=1", prev)
        self.assertIn(f"view={view_id}", prev)
        self.assertIn("size=50", prev)

    def test_view_order_and_widths_apply(self):
        view_id = views.create_view(
            self.conn,
            "订单",
            "列序",
            {"order": ["金额"], "widths": {"金额": 220}, "hidden": ["状态"]},
        )
        self.conn.commit()
        html = self.html(f"/t/订单/?view={view_id}")
        heads = re.findall(
            r'<th class="grid-head" scope="col" style="width: (\d+)px">([^<]+)</th>', html
        )
        names = [name for _, name in heads]
        self.assertEqual(names[:3], ["金额", "单号", "created_at"])
        self.assertNotIn("状态", names)
        self.assertEqual(heads[0][0], "220")

    def test_auxiliary_columns_show_local_time(self):
        self.conn.execute(
            'INSERT INTO "订单" ("单号", "金额", "状态", "created_at", "updated_at")'
            " VALUES (?, ?, ?, ?, ?)",
            ("SO-OLD", 1, "已发货", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        self.conn.commit()
        html = self.html("/t/订单/")
        expected = datetime(2026, 1, 1, tzinfo=timezone.utc).astimezone().strftime(
            "%Y-%m-%d %H:%M"
        )
        self.assertIn(expected, html)
        # 原始 UTC 值只出现在 data-updated-at 属性里，单元格文本必须是本地时间。
        self.assertNotIn(">2026-01-01T00:00:00Z<", html)

    def test_special_characters_are_escaped(self):
        nasty = '<script>&"\''
        rows.insert_rows(self.conn, "订单", [{"单号": nasty, "金额": 1, "状态": "已发货"}])
        schema.create_table(self.conn, "<script>&", [ColumnSpec("名称", "text")])
        view_id = views.create_view(self.conn, "订单", nasty, {})
        self.conn.commit()

        html = self.html(f"/t/订单/?view={view_id}")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&#34;", html)
        self.assertIn("&#39;", html)
        self.assertIn("&lt;script&gt;&amp;", html)

    def test_table_name_with_slash_is_reachable(self):
        schema.create_table(self.conn, "报表/日", [ColumnSpec("标题", "text")])
        rows.insert_rows(self.conn, "报表/日", [{"标题": "日报"}])
        self.conn.commit()
        html = self.html("/t/报表/日/")
        self.assertIn("报表/日", html)
        self.assertIn("日报", html)


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
        # 导入向导已上线：空库入口直接链到 /import。
        self.assertIn('href="/import"', html)

    def test_missing_table_in_empty_library_stays_404(self):
        response = self.client.get("/t/随便/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("找不到这张表", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
