import sqlite3
import unittest

from local_sql_manager.services import audit
from local_sql_manager.services.audit import get_op
from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.models import ConflictError, ViewConfig
from local_sql_manager.services.rows import (
    get_page,
    insert_rows,
    purge_rows,
    restore_rows,
    soft_delete,
    update_cells,
)
from local_sql_manager.services.schema import ColumnSpec, create_table
from local_sql_manager.services.util import utcnow_iso

OLD_TS = "2020-01-01T00:00:00Z"


class BaseWriteTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        create_table(
            self.conn,
            "订单",
            [ColumnSpec("状态", "select", ("新建", "完成")), ColumnSpec("金额", "number")],
        )
        self.ids = [self.insert_seed("完成", 100), self.insert_seed("新建", 50)]
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def insert_seed(self, status, amount):
        cur = self.conn.execute(
            'INSERT INTO "订单" ("created_at", "updated_at", "状态", "金额") '
            "VALUES (?, ?, ?, ?)",
            (OLD_TS, OLD_TS, status, amount),
        )
        return cur.lastrowid

    def row(self, row_id):
        row = self.conn.execute(
            'SELECT * FROM "订单" WHERE "id" = ?', (row_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def cell(self, row_id, column):
        return self.conn.execute(
            f'SELECT "{column}" FROM "订单" WHERE "id" = ?', (row_id,)
        ).fetchone()[0]

    def updated_at(self, row_id):
        return self.cell(row_id, "updated_at")

    def op(self, op_id):
        return get_op(self.conn, op_id)


class TestInsertRows(BaseWriteTestCase):
    def test_insert_sets_timestamps_and_records_op(self):
        op = insert_rows(self.conn, "订单", [{"状态": "完成", "金额": 30}])
        row = self.row(3)
        self.assertIsNone(row["deleted_at"])
        self.assertRegex(row["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(row["created_at"], row["updated_at"])
        record = self.op(op)
        self.assertEqual(record["kind"], "insert")
        self.assertEqual(record["table"], "订单")
        change = record["payload"]["rows"][0]
        self.assertEqual(change["id"], 3)
        self.assertIsNone(change["before"])
        self.assertEqual(change["after"]["金额"], 30)

    def test_insert_coerces_by_field_type(self):
        insert_rows(self.conn, "订单", [{"状态": "完成", "金额": "42.5"}])
        self.assertEqual(self.cell(3, "金额"), 42.5)

    def test_insert_rejects_unknown_field(self):
        with self.assertRaises(ValueError):
            insert_rows(self.conn, "订单", [{"不存在的列": 1}])

    def test_insert_does_not_commit(self):
        insert_rows(self.conn, "订单", [{"状态": "完成", "金额": 30}])
        self.conn.rollback()
        self.assertIsNone(self.row(3))
        self.assertEqual(audit.history(self.conn, "订单"), [])


class TestUpdateCells(BaseWriteTestCase):
    def test_update_bumps_updated_at_and_records_op(self):
        op = update_cells(self.conn, "订单", {1: {"金额": 99}}, {1: self.updated_at(1)})
        self.assertEqual(self.cell(1, "金额"), 99)
        self.assertNotEqual(self.updated_at(1), OLD_TS)
        record = self.op(op)
        self.assertEqual(record["kind"], "update")
        change = record["payload"]["rows"][0]
        self.assertEqual(change["id"], 1)
        self.assertEqual(change["before"]["金额"], 100)
        self.assertEqual(change["after"]["金额"], 99)

    def test_update_accepts_string_ids_from_json(self):
        update_cells(self.conn, "订单", {"1": {"金额": 77}}, {"1": self.updated_at(1)})
        self.assertEqual(self.cell(1, "金额"), 77)

    def test_update_batches_rows_into_single_op(self):
        expected = {1: self.updated_at(1), 2: self.updated_at(2)}
        op = update_cells(
            self.conn, "订单", {1: {"金额": 1}, 2: {"状态": "完成"}}, expected
        )
        rows = self.op(op)["payload"]["rows"]
        self.assertEqual([row["id"] for row in rows], [1, 2])

    def test_stale_updated_at_conflicts(self):
        with self.assertRaises(ConflictError) as ctx:
            update_cells(
                self.conn, "订单", {1: {"金额": 1}}, {1: "2000-01-01T00:00:00Z"}
            )
        self.assertEqual(ctx.exception.rows[0]["id"], 1)
        self.assertEqual(ctx.exception.rows[0]["金额"], 100)
        self.assertEqual(self.cell(1, "金额"), 100)
        self.assertEqual(audit.history(self.conn, "订单"), [])

    def test_conflict_leaves_earlier_rows_untouched(self):
        expected = {1: self.updated_at(1), 2: "2000-01-01T00:00:00Z"}
        with self.assertRaises(ConflictError):
            update_cells(
                self.conn, "订单", {1: {"金额": 1}, 2: {"金额": 2}}, expected
            )
        self.assertEqual(self.cell(1, "金额"), 100)
        self.assertEqual(self.cell(2, "金额"), 50)

    def test_unknown_field_raises(self):
        with self.assertRaises(ValueError):
            update_cells(self.conn, "订单", {1: {"没有": 1}}, {1: self.updated_at(1)})

    def test_update_coerces_by_field_type(self):
        update_cells(self.conn, "订单", {1: {"金额": "99.5"}}, {1: self.updated_at(1)})
        self.assertEqual(self.cell(1, "金额"), 99.5)

    def test_missing_row_conflicts(self):
        with self.assertRaises(ConflictError) as ctx:
            update_cells(self.conn, "订单", {99: {"金额": 1}}, {99: OLD_TS})
        self.assertEqual(ctx.exception.rows[0]["id"], 99)


class TestSoftDelete(BaseWriteTestCase):
    def test_soft_delete_is_invisible_to_page(self):
        op = soft_delete(self.conn, "订单", [1])
        page = get_page(self.conn, "订单", ViewConfig())
        self.assertEqual(page.total, len(self.ids) - 1)
        row = self.row(1)
        self.assertIsNotNone(row["deleted_at"])
        self.assertNotEqual(row["updated_at"], OLD_TS)
        record = self.op(op)
        self.assertEqual(record["kind"], "delete")
        self.assertIsNone(record["payload"]["rows"][0]["before"]["deleted_at"])
        self.assertIsNotNone(record["payload"]["rows"][0]["after"]["deleted_at"])

    def test_soft_delete_only_touches_given_ids(self):
        soft_delete(self.conn, "订单", [1])
        self.assertIsNone(self.row(2)["deleted_at"])

    def test_soft_delete_skips_already_deleted(self):
        soft_delete(self.conn, "订单", [1])
        op = soft_delete(self.conn, "订单", [1])
        self.assertEqual(self.op(op)["payload"]["rows"], [])


class TestRestoreRows(BaseWriteTestCase):
    def test_restore_clears_deleted_at(self):
        soft_delete(self.conn, "订单", [1])
        op = restore_rows(self.conn, "订单", [1])
        self.assertIsNone(self.row(1)["deleted_at"])
        self.assertEqual(get_page(self.conn, "订单", ViewConfig()).total, 2)
        record = self.op(op)
        self.assertEqual(record["kind"], "restore")
        self.assertIsNotNone(record["payload"]["rows"][0]["before"]["deleted_at"])
        self.assertIsNone(record["payload"]["rows"][0]["after"]["deleted_at"])

    def test_restore_skips_rows_that_are_not_deleted(self):
        op = restore_rows(self.conn, "订单", [1])
        self.assertEqual(self.op(op)["payload"]["rows"], [])

    def test_restore_reports_unique_conflict(self):
        self.conn.execute(
            'CREATE TABLE "客户" ("id" INTEGER PRIMARY KEY, "created_at" TEXT,'
            ' "updated_at" TEXT, "deleted_at" TEXT, "code" TEXT)'
        )
        self.conn.execute(
            'CREATE UNIQUE INDEX "客户_code" ON "客户" ("code")'
            ' WHERE "deleted_at" IS NULL'
        )
        self.conn.execute(
            'INSERT INTO "客户" ("created_at", "updated_at", "code") VALUES (?, ?, ?)',
            (OLD_TS, OLD_TS, "x"),
        )
        self.conn.commit()
        row_id = self.conn.execute('SELECT "id" FROM "客户"').fetchone()[0]
        soft_delete(self.conn, "客户", [row_id])
        self.conn.execute(
            'INSERT INTO "客户" ("created_at", "updated_at", "code") VALUES (?, ?, ?)',
            (utcnow_iso(), utcnow_iso(), "x"),
        )
        with self.assertRaises(ValueError) as ctx:
            restore_rows(self.conn, "客户", [row_id])
        self.assertIn("唯一", str(ctx.exception))
        self.conn.rollback()


class TestPurgeRows(BaseWriteTestCase):
    def test_purge_deletes_row_and_marks_irreversible(self):
        op = purge_rows(self.conn, "订单", [1])
        self.assertIsNone(self.row(1))
        record = self.op(op)
        self.assertEqual(record["kind"], "purge")
        self.assertEqual(record["irreversible"], 1)
        change = record["payload"]["rows"][0]
        self.assertEqual(change["before"]["金额"], 100)
        self.assertIsNone(change["after"])

    def test_purge_persists_after_commit(self):
        purge_rows(self.conn, "订单", [1])
        self.conn.commit()
        self.assertIsNone(self.row(1))
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM "订单"').fetchone()[0], 1
        )


if __name__ == "__main__":
    unittest.main()
