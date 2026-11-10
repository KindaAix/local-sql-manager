import sqlite3
import unittest

from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.schema import (
    ColumnSpec,
    create_table,
    ensure_indexes,
    get_fields,
    list_tables,
)


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_create_table_adds_aux_columns(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        cols = [r["name"] for r in self.conn.execute('PRAGMA table_info("订单")')]
        self.assertEqual(cols[:4], ["id", "created_at", "updated_at", "deleted_at"])
        self.assertIn("金额", cols)

    def test_registers_field_metadata(self):
        create_table(
            self.conn,
            "订单",
            [ColumnSpec("金额", "number"), ColumnSpec("状态", "select", ("新建", "完成"))],
        )
        fields = get_fields(self.conn, "订单")
        self.assertEqual([f.name for f in fields], ["金额", "状态"])
        self.assertEqual(fields[1].type, "select")
        self.assertEqual(fields[1].options, ("新建", "完成"))
        self.assertEqual(fields[0].options, ())

    def test_creates_index_per_business_column(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        idx = [r["name"] for r in self.conn.execute('PRAGMA index_list("订单")')]
        self.assertIn("__lsm_idx_2_订单_2_金额", idx)

    def test_physical_column_types(self):
        create_table(
            self.conn,
            "订单",
            [
                ColumnSpec("名称", "text"),
                ColumnSpec("金额", "number"),
                ColumnSpec("已完成", "checkbox"),
                ColumnSpec("日期", "date"),
            ],
        )
        types = {
            r["name"]: r["type"]
            for r in self.conn.execute('PRAGMA table_info("订单")')
        }
        self.assertEqual(types["名称"], "TEXT")
        self.assertEqual(types["金额"], "REAL")
        self.assertEqual(types["已完成"], "INTEGER")
        self.assertEqual(types["日期"], "TEXT")

    def test_registers_table_row(self):
        create_table(
            self.conn, "订单", [ColumnSpec("金额", "number")], source_file="订单.xlsx"
        )
        row = self.conn.execute(
            'SELECT * FROM "__lsm_table" WHERE "table_name" = ?', ("订单",)
        ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["source_file"], "订单.xlsx")
        self.assertRegex(row["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_field_positions_and_width(self):
        create_table(
            self.conn,
            "订单",
            [ColumnSpec("金额", "number"), ColumnSpec("备注", "text")],
        )
        fields = get_fields(self.conn, "订单")
        self.assertEqual([f.position for f in fields], [0, 1])
        self.assertEqual([f.width for f in fields], [140, 140])

    def test_list_tables_excludes_internal(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        create_table(self.conn, "客户", [ColumnSpec("名称", "text")])
        self.assertEqual(list_tables(self.conn), ["订单", "客户"])

    def test_ensure_indexes_is_idempotent(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        ensure_indexes(self.conn, "订单")
        idx = [r["name"] for r in self.conn.execute('PRAGMA index_list("订单")')]
        self.assertEqual(idx.count("__lsm_idx_2_订单_2_金额"), 1)

    def test_index_names_do_not_collide_across_tables(self):
        create_table(self.conn, "a_b", [ColumnSpec("c", "text")])
        create_table(self.conn, "a", [ColumnSpec("b_c", "text")])
        first = [r["name"] for r in self.conn.execute('PRAGMA index_list("a_b")')]
        second = [r["name"] for r in self.conn.execute('PRAGMA index_list("a")')]
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0], second[0])

    def test_rejects_internal_table_name(self):
        with self.assertRaises(ValueError):
            create_table(self.conn, "__lsm_hidden", [ColumnSpec("金额", "number")])

    def test_rejects_sqlite_table_name(self):
        with self.assertRaises(ValueError):
            create_table(self.conn, "sqlite_hidden", [ColumnSpec("金额", "number")])
        with self.assertRaises(ValueError):
            create_table(self.conn, "SQLite_hidden", [ColumnSpec("金额", "number")])

    def test_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            create_table(self.conn, "订单", [ColumnSpec("金额", "money")])


if __name__ == "__main__":
    unittest.main()
