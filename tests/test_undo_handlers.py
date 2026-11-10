"""撤销导入的集成测试：真实导入 → 撤销 → 回收站/审计。"""

import importlib
import os
import sqlite3
import tempfile
import unittest

import local_sql_manager.services as services
from local_sql_manager.services import audit, undo
from local_sql_manager.services.import_excel import import_sheets
from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.models import StaleOperation
from local_sql_manager.services.rows import insert_rows
from local_sql_manager.services.schema import (
    ColumnSpec,
    create_table,
    drop_table,
    list_tables,
    list_trashed_tables,
)
from tests.fixtures.make_fixtures import make_workbook


class UndoImportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        ensure_meta(self.conn)

    def make(self, name, maker):
        target = os.path.join(self.tmp.name, name)
        maker(target)
        return target

    def run_import(self, sheets=("订单", "客户"), on_conflict="skip"):
        path = self.make("数据.xlsx", make_workbook)
        created = import_sheets(self.conn, path, list(sheets), {}, on_conflict)
        self.conn.commit()
        op = audit.history(self.conn, limit=1)[0]
        self.assertEqual(op["kind"], "import")
        return created, op["id"]


class TestUndoImport(UndoImportTestCase):
    def test_undo_import_trashes_created_tables_and_records_undo(self):
        created, op_id = self.run_import()
        self.assertEqual(created, ["订单", "客户"])

        undo_id = undo.undo(self.conn, op_id)
        self.assertEqual(list_tables(self.conn), [])
        trashed = sorted(item["name"] for item in list_trashed_tables(self.conn))
        self.assertEqual(trashed, sorted(created))

        record = audit.get_op(self.conn, undo_id)
        self.assertEqual(record["kind"], "undo")
        self.assertEqual(record["payload"]["op"], op_id)
        self.assertEqual(record["payload"]["action"], "undo_import")
        self.assertEqual(record["payload"]["tables"], created)
        self.assertEqual(
            [item["table"] for item in record["payload"]["trashed"]], created
        )
        self.assertEqual(record["payload"]["missing"], [])
        self.assertEqual(audit.get_op(self.conn, op_id)["undone_by"], undo_id)

    def test_undo_import_rejects_later_writes(self):
        _, op_id = self.run_import(["订单"])
        insert_rows(self.conn, "订单", [{"编码": "X-1"}])

        with self.assertRaises(StaleOperation) as caught:
            undo.undo(self.conn, op_id)
        self.assertIn("订单", str(caught.exception))
        self.assertIn("订单", list_tables(self.conn))
        self.assertIsNone(audit.get_op(self.conn, op_id)["undone_by"])

    def test_undo_import_skips_missing_tables(self):
        _, op_id = self.run_import(["订单", "客户"])
        drop_table(self.conn, "客户")

        undo_id = undo.undo(self.conn, op_id)
        record = audit.get_op(self.conn, undo_id)
        self.assertEqual(record["payload"]["missing"], ["客户"])
        self.assertEqual(
            [item["table"] for item in record["payload"]["trashed"]], ["订单"]
        )
        self.assertNotIn("订单", list_tables(self.conn))
        self.assertIn(
            "客户", [item["name"] for item in list_trashed_tables(self.conn)]
        )

    def test_undo_import_accepts_both_table_entry_shapes(self):
        create_table(self.conn, "甲", [ColumnSpec("名称", "text")])
        legacy = audit.record(
            self.conn,
            "import",
            "甲",
            "web",
            {"file": "甲.xlsx", "tables": ["甲"]},
        )
        undo.undo(self.conn, legacy)
        self.assertNotIn("甲", list_tables(self.conn))

        create_table(self.conn, "乙", [ColumnSpec("名称", "text")])
        structured = audit.record(
            self.conn,
            "import",
            "乙",
            "web",
            {
                "file": "乙.xlsx",
                "tables": [{"sheet": "乙", "table": "乙", "rows": 1}],
            },
        )
        undo.undo(self.conn, structured)
        self.assertNotIn("乙", list_tables(self.conn))

    def test_undo_import_leaves_overwritten_table_in_trash(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        _, op_id = self.run_import(["订单"], on_conflict="overwrite")
        self.assertEqual(len(list_trashed_tables(self.conn)), 1)

        undo.undo(self.conn, op_id)
        self.assertNotIn("订单", list_tables(self.conn))
        # 旧表仍在回收站，撤销导入不会把它顺手恢复。
        self.assertEqual(len(list_trashed_tables(self.conn)), 2)

    def test_undo_of_undone_import_is_unsupported(self):
        _, op_id = self.run_import(["订单"])
        undo_id = undo.undo(self.conn, op_id)
        with self.assertRaises(StaleOperation):
            undo.undo(self.conn, undo_id)


class TestHandlerRegistration(unittest.TestCase):
    def test_services_package_registers_all_handlers(self):
        expected = {
            "import",
            "merge",
            "create_field",
            "rename_field",
            "drop_field",
            "set_field_type",
        }
        self.assertTrue(expected <= set(undo._HANDLERS))

    def test_reloading_services_is_idempotent(self):
        before = dict(undo._HANDLERS)
        for _ in range(2):
            importlib.reload(services)
        self.assertEqual(set(undo._HANDLERS), set(before))
        self.assertIn("import", undo._HANDLERS)


if __name__ == "__main__":
    unittest.main()
