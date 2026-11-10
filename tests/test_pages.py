"""页面与工具路由测试：导入向导、导出、分享、合并、启用编辑、视图、
历史与回收站。

所有落库断言都用标准库 sqlite3 重新打开库文件核对，不信任路由里的连接。
"""

import io
import os
import re
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from local_sql_manager.app import create_app
from local_sql_manager.routes import import_export
from local_sql_manager.services import audit, rows, schema, views
from local_sql_manager.services.library import open_library
from local_sql_manager.services.schema import ColumnSpec
from local_sql_manager.services.share import export_lsmf
from tests.fixtures.make_fixtures import (
    make_csv,
    make_large_workbook,
    make_workbook,
)


class PagesTest(unittest.TestCase):
    """一张受管的「订单」表（3 行）与一张未受管的「外部表」。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        self.path = os.path.join(tmp.name, "pages.sqlite")
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
        self.library.conn.execute('CREATE TABLE "外部表" ("名称" TEXT NOT NULL)')
        self.library.conn.execute('INSERT INTO "外部表" ("名称") VALUES (?)', ("甲",))
        self.library.conn.execute('INSERT INTO "外部表" ("名称") VALUES (?)', ("乙",))
        self.library.conn.commit()
        self.client = create_app(self.library).test_client()

    # --- 工具 ---------------------------------------------------------

    def fetch(self, sql: str, params=()):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def make_xlsx(self, name: str = "book.xlsx", maker=make_workbook) -> str:
        path = os.path.join(self.tmpdir, name)
        maker(path)
        return path

    def upload(self, path: str, filename: str | None = None):
        name = filename or os.path.basename(path)
        data = {
            "file": (io.BytesIO(Path(path).read_bytes()), name),
        }
        return self.client.post(
            "/import", data=data, content_type="multipart/form-data"
        )

    def token(self, response) -> str:
        match = re.search(
            r'name="token" value="([^"]+)"', response.get_data(as_text=True)
        )
        self.assertIsNotNone(match, "预览页缺少 token")
        return match.group(1)

    def confirm(self, token: str, **fields):
        data = {"token": token, "on_conflict": "skip"}
        data.update(fields)
        return self.client.post("/import/confirm", data=data)

    def table_names(self) -> list[str]:
        conn = sqlite3.connect(self.path)
        try:
            return [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
        finally:
            conn.close()

    # --- 导入向导 -----------------------------------------------------

    def test_import_page_lists_upload_form(self):
        response = self.client.get("/import")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('type="file"', html)
        self.assertIn("导入分享包", html)

    def test_import_upload_previews_sheets(self):
        response = self.upload(self.make_xlsx())
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("订单", html)
        self.assertIn("空表", html)
        self.assertIn("客户", html)
        self.assertIn("目标表名", html)
        self.assertIn("空表，将跳过", html)
        # 空表的复选框与表名输入都应禁用。
        self.assertGreaterEqual(html.count("disabled"), 2)
        self.assertIn("预览导入", html)

    def test_import_csv_confirm(self):
        path = os.path.join(self.tmpdir, "客户.csv")
        make_csv(path)
        token = self.token(self.upload(path, "客户.csv"))
        response = self.confirm(token, sheet_0="客户", name_0="客户名单")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.fetch('SELECT COUNT(*) FROM "客户名单"')[0][0], 2)
        fields = self.fetch(
            'SELECT "column" FROM "__lsm_field" WHERE "table_name" = ?', ("客户名单",)
        )
        self.assertEqual(
            sorted(item[0] for item in fields), sorted(["名称", "金额", "编码"])
        )

    def test_import_preview_marks_sampled_types(self):
        path = self.make_xlsx("large.xlsx", maker=lambda p: make_large_workbook(p, 5001))
        html = self.upload(path).get_data(as_text=True)
        self.assertIn("按前 5000 行推断", html)

    def test_import_confirm_creates_table_and_audit(self):
        token = self.token(self.upload(self.make_xlsx()))
        response = self.confirm(
            token,
            sheet_2="客户",
            name_2="导入客户",
            type_2_0="text",
        )
        self.assertEqual(response.status_code, 302)
        names = self.table_names()
        self.assertIn("导入客户", names)
        self.assertNotIn("客户", names)
        self.assertEqual(
            self.fetch('SELECT COUNT(*) FROM "导入客户"')[0][0], 2
        )
        kinds = [row[0] for row in self.fetch('SELECT "kind" FROM "__lsm_op"')]
        self.assertIn("import", kinds)

    def test_import_confirm_renames_target_table(self):
        token = self.token(self.upload(self.make_xlsx()))
        response = self.confirm(token, sheet_2="客户", name_2="我的客户")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/t/", response.headers["Location"])
        self.assertIn("我的客户", self.table_names())
        self.assertNotIn("客户", self.table_names())

    def test_import_confirm_rejects_empty_selection(self):
        token = self.token(self.upload(self.make_xlsx()))
        response = self.confirm(token, on_conflict="skip")
        self.assertEqual(response.status_code, 302)
        html = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("请至少选择一个工作表", html)

    def test_import_confirm_skip_conflict_keeps_old_table(self):
        token = self.token(self.upload(self.make_xlsx()))
        response = self.confirm(token, sheet_0="订单", name_0="订单")
        self.assertEqual(response.status_code, 400)
        self.assertIn("已跳过", response.get_data(as_text=True))
        self.assertEqual(self.fetch('SELECT COUNT(*) FROM "订单"')[0][0], 3)

    def test_import_confirm_overwrite_moves_old_table_to_trash(self):
        token = self.token(self.upload(self.make_xlsx()))
        response = self.confirm(
            token, sheet_0="订单", name_0="订单", on_conflict="overwrite"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.fetch('SELECT COUNT(*) FROM "订单"')[0][0], 2)
        self.assertTrue(schema.list_trashed_tables(self.library.conn))

    def test_import_expired_token_redirects_with_flash(self):
        response = self.confirm("not-a-token", sheet_0="订单", name_0="订单")
        self.assertEqual(response.status_code, 302)
        html = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("上传已过期", html)

    def test_import_rejects_xls_and_other_suffixes(self):
        xls = self.upload(os.devnull, "旧表.xls")
        self.assertEqual(xls.status_code, 302)
        self.assertIn(
            "另存为 .xlsx",
            self.client.get(xls.headers["Location"]).get_data(as_text=True),
        )
        other = self.upload(self.make_xlsx(), "book.txt")
        self.assertIn(
            "只支持 .xlsx 与 .csv",
            self.client.get(other.headers["Location"]).get_data(as_text=True),
        )

    def test_import_cleans_temp_files_after_confirm(self):
        token = self.token(self.upload(self.make_xlsx()))
        directory = Path(import_export._pending[token]["dir"])
        self.assertTrue(directory.exists())
        self.confirm(token, sheet_2="客户", name_2="客户")
        self.assertNotIn(token, import_export._pending)
        self.assertFalse(directory.exists())

    def test_import_cancel_cleans_temp_files(self):
        token = self.token(self.upload(self.make_xlsx()))
        directory = Path(import_export._pending[token]["dir"])
        response = self.client.post("/import/cancel", data={"token": token})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(token, import_export._pending)
        self.assertFalse(directory.exists())

    # --- 导出与分享 ---------------------------------------------------

    def test_export_xlsx_returns_download(self):
        response = self.client.get("/export/订单?format=xlsx&view=")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("%E8%AE%A2%E5%8D%95", response.headers["Content-Disposition"])
        self.assertTrue(response.data.startswith(b"PK"))

    def test_export_csv_returns_bom_and_header(self):
        response = self.client.get("/export/订单?format=csv")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("text/csv"))
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("单号".encode("utf-8"), response.data)

    def test_export_missing_table_flashes(self):
        response = self.client.get("/export/不存在?format=xlsx", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("不存在", response.get_data(as_text=True))

    def test_share_export_returns_lsmf(self):
        response = self.client.get("/share/export/订单")
        self.assertEqual(response.status_code, 200)
        self.assertIn(".lsmf", response.headers["Content-Disposition"])
        self.assertTrue(response.data.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(response.data)) as zf:
            self.assertIn("manifest.json", zf.namelist())

    def test_share_import_creates_new_library(self):
        package = os.path.join(self.tmpdir, "分享.lsmf")
        export_lsmf(self.library.conn, self.path, package, "0.1.0")
        data = {"file": (io.BytesIO(Path(package).read_bytes()), "分享.lsmf")}
        response = self.client.post(
            "/share/import", data=data, content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("分享包已导入为新库", response.get_data(as_text=True))
        imported = os.path.join(self.tmpdir, "分享.sqlite")
        self.assertTrue(Path(imported).exists())
        conn = sqlite3.connect(imported)
        try:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ]
        finally:
            conn.close()
        self.assertIn("订单", names)

    # --- 合并与启用编辑 -----------------------------------------------

    def make_second_table(self):
        schema.create_table(
            self.library.conn,
            "订单二",
            [
                ColumnSpec("单号", "text"),
                ColumnSpec("金额", "number"),
                ColumnSpec("状态", "select"),
            ],
        )
        rows.insert_rows(
            self.library.conn,
            "订单二",
            [{"单号": "SO-2026-0151", "金额": 50, "状态": "已发货"}],
        )
        self.library.conn.commit()

    def test_merge_preview_shows_plan(self):
        self.make_second_table()
        response = self.client.post(
            "/merge/preview",
            data={"tables": ["订单", "订单二"], "target": "全部订单"},
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("共 4 行", html)
        self.assertIn("全部订单", html)
        self.assertIn("目标列", html)

    def test_merge_confirm_creates_target(self):
        self.make_second_table()
        response = self.client.post(
            "/merge/confirm",
            data={"tables": ["订单", "订单二"], "target": "全部订单"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("全部订单", self.table_names())
        self.assertEqual(self.fetch('SELECT COUNT(*) FROM "全部订单"')[0][0], 4)

    def test_adopt_page_shows_plan(self):
        response = self.client.get("/adopt/外部表")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("将新增 id 列", html)
        self.assertIn("确认启用编辑", html)
        self.assertIn("名称", html)

    def test_adopt_confirm_makes_table_editable(self):
        response = self.client.post("/adopt/外部表/confirm", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("已启用编辑", response.get_data(as_text=True))
        row = self.fetch(
            'SELECT "status" FROM "__lsm_table" WHERE "table_name" = ?', ("外部表",)
        )
        self.assertEqual(row[0][0], "active")
        fields = self.fetch(
            'SELECT "column" FROM "__lsm_field" WHERE "table_name" = ?', ("外部表",)
        )
        self.assertIn("名称", [item[0] for item in fields])

    def test_adopt_rejects_managed_table(self):
        response = self.client.get("/adopt/订单", follow_redirects=True)
        self.assertIn("已启用编辑", response.get_data(as_text=True))

    # --- 视图 ---------------------------------------------------------

    def test_view_api_lifecycle(self):
        response = self.client.post(
            "/api/views",
            json={
                "table": "订单",
                "name": "本月",
                "config": {"sorts": [{"column": "金额", "dir": "desc"}]},
            },
        )
        self.assertEqual(response.status_code, 201)
        view_id = response.get_json()["id"]

        updated = self.client.post(
            "/api/views/%d" % view_id,
            json={
                "name": "本月已发货",
                "config": {
                    "filters": [{"column": "金额", "op": "gt", "value": 100}],
                    "sorts": [{"column": "金额", "dir": "desc"}],
                },
            },
        )
        self.assertEqual(updated.status_code, 200)
        stored = self.fetch(
            'SELECT "name", "config_json" FROM "__lsm_view" WHERE "id" = ?',
            (view_id,),
        )[0]
        self.assertEqual(stored[0], "本月已发货")
        self.assertIn("gt", stored[1])

        other = self.client.post(
            "/api/views", json={"table": "订单", "name": "另一个", "config": {}}
        )
        self.assertEqual(other.status_code, 201)
        other_id = other.get_json()["id"]

        default = self.client.post("/api/views/%d/default" % view_id, json={})
        self.assertEqual(default.status_code, 200)
        self.assertEqual(
            self.fetch(
                'SELECT "is_default" FROM "__lsm_view" WHERE "id" = ?', (view_id,)
            )[0][0],
            1,
        )
        self.assertEqual(
            self.fetch(
                'SELECT "is_default" FROM "__lsm_view" WHERE "id" = ?', (other_id,)
            )[0][0],
            0,
        )

        deleted = self.client.delete("/api/views/%d" % other_id)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            self.fetch('SELECT "id" FROM "__lsm_view" WHERE "id" = ?', (other_id,)), []
        )
        # 默认视图不能删除。
        blocked = self.client.delete("/api/views/%d" % view_id)
        self.assertEqual(blocked.status_code, 400)

    def test_view_default_cannot_be_deleted(self):
        response = self.client.post(
            "/api/views", json={"table": "订单", "name": "默认视图", "config": {}}
        )
        self.assertEqual(response.status_code, 201)
        view_id = response.get_json()["id"]
        deleted = self.client.delete("/api/views/%d" % view_id)
        self.assertEqual(deleted.status_code, 400)
        self.assertIn("默认视图不能删除", deleted.get_json()["error"])

    def test_view_requires_managed_table(self):
        response = self.client.post(
            "/api/views", json={"table": "外部表", "name": "只读", "config": {}}
        )
        self.assertEqual(response.status_code, 403)

    def test_view_panel_rendered_on_grid(self):
        html = self.client.get("/t/订单/").get_data(as_text=True)
        self.assertIn('id="view-panel"', html)
        self.assertIn("添加筛选", html)
        self.assertIn("列顺序", html)
        self.assertIn("分组与汇总", html)
        self.assertIn("另存为新视图", html)
        self.assertIn("当前是默认视图", html)

        created = self.client.post(
            "/api/views", json={"table": "订单", "name": "已保存视图", "config": {}}
        )
        view_id = created.get_json()["id"]
        html = self.client.get("/t/订单/?view=%d" % view_id).get_data(as_text=True)
        self.assertIn("设为默认", html)
        self.assertIn(">保存<", html)
        self.assertIn("已保存视图", html)

    def test_view_panel_hidden_for_unmanaged_table(self):
        response = self.client.get("/t/外部表/")
        html = response.get_data(as_text=True)
        self.assertNotIn('id="view-panel"', html)
        self.assertIn("启用编辑", html)

    # --- 历史 ---------------------------------------------------------

    def test_history_lists_operations_and_undo(self):
        op_id = self.fetch('SELECT "id" FROM "__lsm_op" ORDER BY "id" DESC')[0][0]
        html = self.client.get("/history").get_data(as_text=True)
        self.assertIn("新增", html)
        self.assertIn("界面", html)
        self.assertIn("可回退", html)

        response = self.client.post(
            "/history/undo/%d" % op_id, data={"table": "订单", "limit": 50},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已回退操作", response.get_data(as_text=True))
        self.assertEqual(self.fetch('SELECT COUNT(*) FROM "订单"')[0][0], 0)
        self.assertIn("已撤销", self.client.get("/history").get_data(as_text=True))

    def test_history_undo_failure_flashes(self):
        op_id = self.fetch('SELECT "id" FROM "__lsm_op" ORDER BY "id" DESC')[0][0]
        self.client.post("/history/undo/%d" % op_id, data={"table": "订单"})
        response = self.client.post(
            "/history/undo/%d" % op_id, data={"table": "订单"}, follow_redirects=True
        )
        self.assertIn("已被撤销", response.get_data(as_text=True))

    # --- 回收站 -------------------------------------------------------

    def soft_delete_first(self):
        response = self.client.post(
            "/api/t/订单/delete", json={"ids": [1]}
        )
        self.assertEqual(response.status_code, 200)

    def test_trash_lists_soft_deleted_rows(self):
        self.soft_delete_first()
        html = self.client.get("/trash").get_data(as_text=True)
        self.assertIn("SO-2026-0148", html)
        self.assertIn("彻底删除", html)
        self.assertIn("恢复", html)

    def test_trash_restore_row_via_form(self):
        self.soft_delete_first()
        response = self.client.post(
            "/api/trash/restore",
            data={"table": "订单", "ids": ["1"]},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已恢复 1 行", response.get_data(as_text=True))
        self.assertIsNone(
            self.fetch('SELECT "deleted_at" FROM "订单" WHERE "id" = 1')[0][0]
        )

    def test_trash_purge_row_via_json(self):
        self.soft_delete_first()
        response = self.client.post(
            "/api/trash/purge", json={"table": "订单", "ids": [1]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 1)
        self.assertEqual(self.fetch('SELECT "id" FROM "订单" WHERE "id" = 1'), [])

    def test_trash_tables_restore_and_purge(self):
        schema.drop_table(self.library.conn, "订单")
        self.library.conn.commit()
        html = self.client.get("/trash").get_data(as_text=True)
        self.assertIn("已删除的表", html)
        self.assertIn("订单", html)

        restored = self.client.post(
            "/api/trash/tables/restore", json={"name": "订单"}
        )
        self.assertEqual(restored.status_code, 200)
        self.assertIn("订单", self.table_names())

        schema.drop_table(self.library.conn, "订单")
        self.library.conn.commit()
        purged = self.client.post("/api/trash/tables/purge", json={"name": "订单"})
        self.assertEqual(purged.status_code, 200)
        self.assertNotIn("订单", self.table_names())
        self.assertEqual(
            self.fetch(
                'SELECT "table_name" FROM "__lsm_table" WHERE "table_name" = ?',
                ("订单",),
            ),
            [],
        )

    # --- 只读表 -------------------------------------------------------

    def test_unmanaged_table_shows_readonly_without_write_entries(self):
        html = self.client.get("/t/外部表/").get_data(as_text=True)
        self.assertIn("只读", html)
        self.assertIn("启用编辑", html)
        self.assertNotIn("导出 Excel", html)
        self.assertNotIn("视图设置", html)
        self.assertNotIn("添加筛选", html)
        self.assertNotIn("新建视图", html)

    def test_managed_table_shows_export_and_panel_entries(self):
        html = self.client.get("/t/订单/").get_data(as_text=True)
        self.assertIn("导出 Excel", html)
        self.assertIn("分享", html)
        self.assertIn("视图设置", html)
        self.assertIn("新建视图", html)
        self.assertNotIn("只读", html)


if __name__ == "__main__":
    unittest.main()
