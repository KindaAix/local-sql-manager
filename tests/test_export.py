"""Excel 与 CSV 导出。"""

import base64
import csv
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from local_sql_manager.services import export_excel
from local_sql_manager.services.export_excel import (
    export_csv,
    export_table,
    export_view,
    export_workbook,
)
from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.models import ViewConfig
from local_sql_manager.services.schema import ColumnSpec, create_table
from local_sql_manager.services.util import utcnow_iso


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        create_table(
            self.conn,
            "订单",
            [
                ColumnSpec("名称", "text"),
                ColumnSpec("金额", "number"),
                ColumnSpec("状态", "select", ("新建", "完成")),
                ColumnSpec("日期", "date"),
            ],
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def path(self, name="out.xlsx"):
        return Path(self.tmp.name) / name

    def insert(self, name, amount, status, day="2026-11-01", deleted_at=None):
        now = utcnow_iso()
        self.conn.execute(
            'INSERT INTO "订单" ("created_at", "updated_at", "deleted_at",'
            ' "名称", "金额", "状态", "日期") VALUES (?, ?, ?, ?, ?, ?, ?)',
            (now, now, deleted_at, name, amount, status, day),
        )
        self.conn.commit()

    def sheet(self, path, title=None):
        wb = load_workbook(path, read_only=True)
        self.addCleanup(wb.close)
        return wb[title] if title is not None else wb.worksheets[0]

    def rows(self, worksheet):
        return [list(row) for row in worksheet.iter_rows(values_only=True)]

    def test_export_view_applies_filters_and_hidden_columns(self):
        self.insert("甲", 10, "完成")
        self.insert("乙", 20, "新建")
        self.insert("丙", 30, "完成")
        view = ViewConfig(
            filters=[{"column": "状态", "op": "eq", "value": "完成"}],
            hidden=["状态"],
        )
        out = export_view(self.conn, "订单", view, self.path())
        data = self.rows(self.sheet(out))
        self.assertNotIn("状态", data[0])
        self.assertEqual(data[0][:2], ["名称", "金额"])
        self.assertEqual(len(data), 3)  # 表头 + 2 条命中
        self.assertEqual([row[0] for row in data[1:]], ["甲", "丙"])

    def test_export_default_view_has_aux_columns_without_id(self):
        self.insert("甲", 10, "完成")
        out = export_table(self.conn, "订单", self.path())
        header = self.rows(self.sheet(out))[0]
        self.assertEqual(header[:4], ["名称", "金额", "状态", "日期"])
        self.assertEqual(header[-2:], ["created_at", "updated_at"])
        self.assertNotIn("id", header)
        self.assertNotIn("deleted_at", header)

    def test_soft_deleted_rows_are_not_exported(self):
        self.insert("甲", 10, "完成")
        self.insert("乙", 20, "完成", deleted_at=utcnow_iso())
        out = export_table(self.conn, "订单", self.path())
        data = self.rows(self.sheet(out))
        self.assertEqual(len(data), 2)
        self.assertEqual(data[1][0], "甲")

    def test_view_order_moves_columns_to_front(self):
        self.insert("甲", 10, "完成")
        view = ViewConfig(order=["金额", "名称"])
        out = export_view(self.conn, "订单", view, self.path())
        header = self.rows(self.sheet(out))[0]
        self.assertEqual(header[:2], ["金额", "名称"])
        self.assertEqual(header[2:4], ["状态", "日期"])

    def test_view_dict_is_accepted(self):
        self.insert("甲", 10, "完成")
        out = export_view(
            self.conn, "订单", {"hidden": ["状态"]}, self.path()
        )
        self.assertNotIn("状态", self.rows(self.sheet(out))[0])

    def test_sorting_is_applied(self):
        for name, amount in (("甲", 10), ("乙", 30), ("丙", 20)):
            self.insert(name, amount, "完成")
        view = ViewConfig(sorts=[{"column": "金额", "dir": "desc"}])
        out = export_view(self.conn, "订单", view, self.path())
        data = self.rows(self.sheet(out))
        self.assertEqual([row[1] for row in data[1:]], [30, 20, 10])

    def test_missing_parent_directory_is_created(self):
        self.insert("甲", 10, "完成")
        out = export_view(
            self.conn, "订单", None, self.path("嵌套/目录/out.xlsx")
        )
        self.assertTrue(out.exists())
        self.assertIsInstance(out, Path)

    def test_max_rows_raises_value_error(self):
        for index in range(3):
            self.insert(f"行{index}", index, "完成")
        original = export_excel.MAX_ROWS
        export_excel.MAX_ROWS = 2
        self.addCleanup(setattr, export_excel, "MAX_ROWS", original)
        with self.assertRaises(ValueError) as ctx:
            export_table(self.conn, "订单", self.path())
        self.assertIn("2", str(ctx.exception))
        self.assertIn("请先筛选", str(ctx.exception))

    def test_bytes_values_are_base64_encoded(self):
        self.insert("甲", 10, "完成")
        self.conn.execute(
            'UPDATE "订单" SET "名称" = ?', (sqlite3.Binary(b"raw-bytes"),)
        )
        self.conn.commit()
        out = export_table(self.conn, "订单", self.path())
        self.assertEqual(
            self.rows(self.sheet(out))[1][0], base64.b64encode(b"raw-bytes").decode()
        )


class WorkbookTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def make_table(self, name, extra=()):
        create_table(self.conn, name, [ColumnSpec("名称", "text"), *extra])
        now = utcnow_iso()
        self.conn.execute(
            f'INSERT INTO "{name}" ("created_at", "updated_at", "名称") VALUES (?, ?, ?)',
            (now, now, "值"),
        )
        self.conn.commit()

    def test_one_sheet_per_table_in_order(self):
        self.make_table("表一")
        self.make_table("表二")
        out = export_workbook(
            self.conn, ["表一", "表二"], Path(self.tmp.name) / "多表.xlsx"
        )
        wb = load_workbook(out, read_only=True)
        self.addCleanup(wb.close)
        self.assertEqual(wb.sheetnames, ["表一", "表二"])
        header = [cell for cell in next(wb["表二"].iter_rows(values_only=True))]
        self.assertEqual(header[0], "名称")

    def test_illegal_characters_and_duplicates_are_fixed(self):
        self.make_table("订单:明细")
        self.make_table("订单/明细")
        out = export_workbook(
            self.conn, ["订单:明细", "订单/明细"], Path(self.tmp.name) / "表名.xlsx"
        )
        wb = load_workbook(out, read_only=True)
        self.addCleanup(wb.close)
        self.assertEqual(wb.sheetnames, ["订单明细", "订单明细-2"])

    def test_long_sheet_name_is_truncated_to_31(self):
        name = "很长的表名" * 8
        self.make_table(name)
        out = export_workbook(self.conn, [name], Path(self.tmp.name) / "长名.xlsx")
        wb = load_workbook(out, read_only=True)
        self.addCleanup(wb.close)
        self.assertEqual(len(wb.sheetnames[0]), 31)
        self.assertEqual(wb.sheetnames[0], name[:31])


class CsvTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        create_table(
            self.conn,
            "订单",
            [
                ColumnSpec("名称", "text"),
                ColumnSpec("金额", "number"),
                ColumnSpec("状态", "select", ("新建", "完成")),
            ],
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def insert(self, name, amount, status, deleted_at=None):
        now = utcnow_iso()
        self.conn.execute(
            'INSERT INTO "订单" ("created_at", "updated_at", "deleted_at",'
            ' "名称", "金额", "状态") VALUES (?, ?, ?, ?, ?, ?)',
            (now, now, deleted_at, name, amount, status),
        )
        self.conn.commit()

    def read(self, path):
        raw = Path(path).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn("\r\n", text)
        return list(csv.reader(io.StringIO(text)))

    def test_bom_and_given_column_order(self):
        self.insert("甲", 10.5, "完成")
        out = export_csv(
            self.conn, "订单", ["金额", "名称"], Path(self.tmp.name) / "a.csv"
        )
        data = self.read(out)
        self.assertEqual(data[0], ["金额", "名称"])
        self.assertEqual(data[1], ["10.5", "甲"])

    def test_empty_columns_falls_back_to_all_business_columns(self):
        self.insert("甲", 10, None)
        out = export_csv(
            self.conn, "订单", [], Path(self.tmp.name) / "b.csv"
        )
        data = self.read(out)
        self.assertEqual(data[0][:3], ["名称", "金额", "状态"])
        self.assertEqual(data[0][-2:], ["created_at", "updated_at"])
        self.assertEqual(data[1][2], "")  # None 写空串

    def test_soft_deleted_rows_are_not_exported(self):
        self.insert("甲", 10, "完成")
        self.insert("乙", 20, "完成", deleted_at=utcnow_iso())
        out = export_csv(
            self.conn, "订单", None, Path(self.tmp.name) / "c.csv"
        )
        data = self.read(out)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[1][0], "甲")
