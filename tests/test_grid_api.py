"""网格写入 API 测试：保存、新增、批量删除、撤销与视图列配置。

所有断言都用标准库 sqlite3 重新打开库文件核对，不信任路由里的连接。
"""

import os
import sqlite3
import tempfile
import unittest

from local_sql_manager.app import create_app
from local_sql_manager.services import audit, rows, schema, views
from local_sql_manager.services.library import open_library
from local_sql_manager.services.schema import ColumnSpec


class GridApiTest(unittest.TestCase):
    """一张受管的「订单」表，三条数据。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "api.sqlite")
        self.library = open_library(self.path)
        self.addCleanup(self.library.conn.close)
        schema.create_table(
            self.library.conn,
            "订单",
            [
                ColumnSpec("单号", "text"),
                ColumnSpec("金额", "number"),
                ColumnSpec("状态", "select"),
            ],
        )
        rows.insert_rows(
            self.library.conn,
            "订单",
            [
                {"单号": "SO-2026-0148", "金额": 520, "状态": "已发货"},
                {"单号": "SO-2026-0149", "金额": 80, "状态": "已发货"},
                {"单号": "SO-2026-0150", "金额": 100, "状态": "待付款"},
            ],
        )
        self.library.conn.commit()
        self.client = create_app(self.library).test_client()

    # --- 工具 ---------------------------------------------------------

    def row(self, row_id: int) -> dict | None:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            found = conn.execute(
                'SELECT * FROM "订单" WHERE "id" = ?', (row_id,)
            ).fetchone()
            return dict(found) if found is not None else None
        finally:
            conn.close()

    def row_count(self) -> int:
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute('SELECT COUNT(*) FROM "订单"').fetchone()[0]
        finally:
            conn.close()

    def op_count(self) -> int:
        conn = sqlite3.connect(self.path)
        try:
            return conn.execute('SELECT COUNT(*) FROM "__lsm_op"').fetchone()[0]
        finally:
            conn.close()

    def version(self, row_id: int) -> str:
        row = self.row(row_id)
        self.assertIsNotNone(row, f"第 {row_id} 行应存在")
        return row["updated_at"]

    def post(self, path: str, payload):
        return self.client.post(path, json=payload)

    def save(self, changes, expected):
        return self.post(
            "/api/t/订单/save", {"changes": changes, "expected": expected}
        )

    # --- 页面脚本与行片段 ---------------------------------------------

    def test_grid_page_includes_editor_script_and_row_metadata(self):
        html = self.client.get("/t/订单/").get_data(as_text=True)
        self.assertIn("/static/js/grid.js", html)
        self.assertIn('data-col="单号"', html)
        self.assertIn('data-updated-at="', html)
        # 版本值做了 URL 编码，源码里不应出现裸的 UTC ISO 串。
        self.assertNotIn("2026-01-01T00:00:00Z", html)

        script = self.client.get("/static/js/grid.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("POST", script.get_data(as_text=True))

    # --- 保存 ---------------------------------------------------------

    def test_save_updates_cell_and_returns_row_fragment(self):
        response = self.save({"1": {"单号": "SO-NEW"}}, {"1": self.version(1)})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("text/html"))
        html = response.get_data(as_text=True)
        self.assertIn('data-id="1"', html)
        self.assertIn('data-col="单号"', html)
        self.assertIn("SO-NEW", html)
        self.assertNotIn("SO-2026-0148", html)
        self.assertNotIn("2026-01-01T00:00:00Z", html)

        self.assertEqual(self.row(1)["单号"], "SO-NEW")
        self.assertGreater(self.row(1)["updated_at"], "2020")

        history = audit.history(self.library.conn, "订单", limit=1)
        self.assertEqual(history[0]["kind"], "update")
        self.assertEqual(history[0]["source"], "web")

    def test_save_multiple_rows_returns_one_fragment_per_row(self):
        version_one, version_two = self.version(1), self.version(2)
        response = self.save(
            {"1": {"状态": "待付款"}, "2": {"状态": "待付款"}},
            {"1": version_one, "2": version_two},
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count('class="grid-row"'), 2)
        self.assertIn('data-id="1"', html)
        self.assertIn('data-id="2"', html)
        self.assertEqual(self.row(1)["状态"], "待付款")
        self.assertEqual(self.row(2)["状态"], "待付款")
        # 两个行的一次保存 = 一条审计，撤销时一次回退。
        self.assertEqual(audit.history(self.library.conn, "订单", limit=1)[0]["kind"], "update")

    def test_save_stale_version_conflicts_without_writing(self):
        ops_before = self.op_count()
        response = self.save(
            {"1": {"单号": "SO-NEW"}}, {"1": "1999-01-01T00:00:00Z"}
        )

        self.assertEqual(response.status_code, 409)
        body = response.get_json()
        self.assertEqual(body["error"], "该记录已被修改，请重新加载")
        self.assertEqual(body["conflict"][0]["id"], 1)
        self.assertEqual(body["conflict"][0]["单号"], "SO-2026-0148")

        self.assertEqual(self.row(1)["单号"], "SO-2026-0148")
        self.assertEqual(self.op_count(), ops_before)

    def test_save_fragment_follows_view_column_order(self):
        view_id = views.create_view(
            self.library.conn,
            "订单",
            "列序",
            {"order": ["金额", "单号"], "hidden": ["状态"]},
        )
        self.library.conn.commit()

        response = self.client.post(
            f"/api/t/订单/save?view={view_id}",
            json={
                "changes": {"1": {"金额": 521}},
                "expected": {"1": self.version(1)},
            },
        )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertLess(
            html.find('data-col="金额"'), html.find('data-col="单号"')
        )
        self.assertNotIn('data-col="状态"', html)
        self.assertEqual(self.row(1)["金额"], 521)

    def test_save_partial_conflict_rolls_back_whole_batch(self):
        ops_before = self.op_count()
        response = self.save(
            {"1": {"单号": "SO-A"}, "2": {"单号": "SO-B"}},
            {"1": self.version(1), "2": "1999-01-01T00:00:00Z"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.row(1)["单号"], "SO-2026-0148")
        self.assertEqual(self.row(2)["单号"], "SO-2026-0149")
        self.assertEqual(self.op_count(), ops_before)

    def test_save_requires_expected_version(self):
        ops_before = self.op_count()
        response = self.post(
            "/api/t/订单/save", {"changes": {"1": {"单号": "X"}}}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("updated_at", response.get_json()["error"])
        self.assertEqual(self.row(1)["单号"], "SO-2026-0148")
        self.assertEqual(self.op_count(), ops_before)

    def test_save_unknown_column_is_bad_request(self):
        ops_before = self.op_count()
        response = self.save({"1": {"不存在的列": "x"}}, {"1": self.version(1)})

        self.assertEqual(response.status_code, 400)
        self.assertIn("未知列", response.get_json()["error"])
        self.assertEqual(self.op_count(), ops_before)
        self.assertEqual(self.row(1)["单号"], "SO-2026-0148")

    def test_save_invalid_number_is_readable_400(self):
        response = self.save({"1": {"金额": "abc"}}, {"1": self.version(1)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "请输入有效的数字")
        self.assertEqual(self.row(1)["金额"], 520)

    def test_sqlite_error_maps_to_500_and_rolls_back(self):
        self.library.conn.execute(
            'CREATE UNIQUE INDEX "uniq_订单_单号" ON "订单" ("单号")'
        )
        self.library.conn.commit()
        ops_before = self.op_count()

        response = self.save(
            {"1": {"单号": "SO-2026-0149"}}, {"1": self.version(1)}
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], "写入失败，请重试")
        self.assertEqual(self.row(1)["单号"], "SO-2026-0148")
        self.assertEqual(self.op_count(), ops_before)

    def test_save_rejects_non_json_request(self):
        response = self.client.post(
            "/api/t/订单/save", data="{}", content_type="text/plain"
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.get_json()["error"], "请求格式必须是 JSON")

    def test_save_rejects_json_array(self):
        response = self.client.post("/api/t/订单/save", json=[1, 2])
        self.assertEqual(response.status_code, 400)

    # --- 新增 ---------------------------------------------------------

    def test_insert_returns_new_row_fragment(self):
        response = self.post(
            "/api/t/订单/insert",
            {"values": {"单号": "SO-NEW", "金额": 7, "状态": "待付款"}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.mimetype.startswith("text/html"))
        html = response.get_data(as_text=True)
        self.assertIn("SO-NEW", html)
        self.assertRegex(html, r'data-id="\d+"')
        self.assertEqual(self.row_count(), 4)

        new_id = int(html.split('data-id="', 1)[1].split('"', 1)[0])
        self.assertEqual(self.row(new_id)["单号"], "SO-NEW")
        history = audit.history(self.library.conn, "订单", limit=1)
        self.assertEqual(history[0]["kind"], "insert")
        self.assertEqual(history[0]["source"], "web")

    def test_insert_allows_empty_values(self):
        response = self.post("/api/t/订单/insert", {"values": {}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.row_count(), 4)

    # --- 删除与撤销 ---------------------------------------------------

    def test_delete_returns_op_and_undo_restores_rows(self):
        response = self.post("/api/t/订单/delete", {"ids": [1, 2]})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["deleted"], 2)
        self.assertIsInstance(body["op"], int)

        self.assertIsNotNone(self.row(1)["deleted_at"])
        self.assertIsNotNone(self.row(2)["deleted_at"])

        undone = self.post(f"/api/undo/{body['op']}", {})
        self.assertEqual(undone.status_code, 200)
        data = undone.get_json()
        self.assertEqual(data["undone"], body["op"])
        self.assertIsInstance(data["op"], int)

        self.assertIsNone(self.row(1)["deleted_at"])
        self.assertIsNone(self.row(2)["deleted_at"])
        self.assertEqual(self.row(1)["单号"], "SO-2026-0148")

    def test_delete_requires_ids(self):
        response = self.post("/api/t/订单/delete", {"ids": []})
        self.assertEqual(response.status_code, 400)
        self.assertIn("没有选择", response.get_json()["error"])

    def test_undo_irreversible_operation_is_409(self):
        self.post("/api/t/订单/delete", {"ids": [1]})
        purge_op = rows.purge_rows(self.library.conn, "订单", [1])
        self.library.conn.commit()

        response = self.post(f"/api/undo/{purge_op}", {})
        self.assertEqual(response.status_code, 409)
        self.assertIn("不可撤销", response.get_json()["error"])
        self.assertIsNone(self.row(1))

    def test_undo_unknown_operation_is_404(self):
        response = self.post("/api/undo/9999", {})
        self.assertEqual(response.status_code, 404)
        self.assertIn("操作不存在", response.get_json()["error"])

    # --- 表校验 -------------------------------------------------------

    def test_unknown_table_is_404(self):
        response = self.post(
            "/api/t/不存在/save", {"changes": {"1": {"A": "x"}}, "expected": {"1": "v"}}
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("表不存在", response.get_json()["error"])

    def test_internal_table_is_404(self):
        response = self.post(
            "/api/t/__lsm_op/delete", {"ids": [1]}
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("表不存在", response.get_json()["error"])

    # --- 视图配置 -----------------------------------------------------

    def test_view_config_saves_widths_and_order(self):
        view_id = views.create_view(self.library.conn, "订单", "列配置", {})
        self.library.conn.commit()

        response = self.post(
            f"/api/views/{view_id}/config",
            {"widths": {"单号": 220}, "order": ["金额", "单号"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

        config = views.load_config(self.library.conn, view_id)
        self.assertEqual(config.widths.get("单号"), 220)
        self.assertEqual(config.order, ["金额", "单号"])

    def test_view_config_merges_existing_widths(self):
        view_id = views.create_view(
            self.library.conn, "订单", "列配置", {"widths": {"单号": 180}}
        )
        self.library.conn.commit()

        self.post(f"/api/views/{view_id}/config", {"widths": {"金额": 260}})
        config = views.load_config(self.library.conn, view_id)
        self.assertEqual(config.widths.get("单号"), 180)
        self.assertEqual(config.widths.get("金额"), 260)

    def test_view_config_unknown_view_is_404(self):
        response = self.post(
            "/api/views/9999/config", {"widths": {"单号": 200}}
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("视图不存在", response.get_json()["error"])

    def test_view_config_default_view_is_400(self):
        response = self.post("/api/views/0/config", {"widths": {"单号": 200}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("默认视图", response.get_json()["error"])

    def test_view_config_rejects_bad_width(self):
        view_id = views.create_view(self.library.conn, "订单", "列配置", {})
        self.library.conn.commit()
        response = self.post(
            f"/api/views/{view_id}/config", {"widths": {"单号": 10}}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("列宽", response.get_json()["error"])

    def test_view_config_requires_json(self):
        view_id = views.create_view(self.library.conn, "订单", "列配置", {})
        self.library.conn.commit()
        response = self.client.post(
            f"/api/views/{view_id}/config",
            data="{}",
            content_type="text/plain",
        )
        self.assertEqual(response.status_code, 415)


if __name__ == "__main__":
    unittest.main()