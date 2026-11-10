import sqlite3
import unittest

from local_sql_manager.services.audit import get_op
from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.schema import (
    ColumnSpec,
    create_table,
    drop_table,
    get_fields,
    list_tables,
    list_trashed_tables,
    purge_table,
    rename_table,
    restore_table,
)
from local_sql_manager.services.util import utcnow_iso


class TestTableOps(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        create_table(
            self.conn,
            "订单",
            [ColumnSpec("金额", "number"), ColumnSpec("备注", "text")],
        )
        now = utcnow_iso()
        self.conn.execute(
            'INSERT INTO "订单" ("created_at", "updated_at", "金额", "备注")'
            " VALUES (?, ?, ?, ?)",
            (now, now, 100, "首单"),
        )
        self.conn.execute(
            'INSERT INTO "__lsm_view" ("table_name", "name", "config_json", "position",'
            ' "is_default") VALUES (?, ?, ?, ?, ?)',
            ("订单", "全部记录", "{}", 0, 1),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def count(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]

    def test_drop_renames_into_trash(self):
        drop_table(self.conn, "订单")
        self.assertIn("订单", [t["name"] for t in list_trashed_tables(self.conn)])
        self.assertNotIn("__lsm_trash_", " ".join(list_tables(self.conn)))

    def test_drop_keeps_data_and_moves_metadata(self):
        op = drop_table(self.conn, "订单")
        trashed = list_trashed_tables(self.conn)[0]
        self.assertEqual(trashed["table_name"], f"__lsm_trash_{op}_订单")
        self.assertEqual(trashed["trashed_op"], op)

        physical = trashed["table_name"]
        self.assertEqual(self.count(f'SELECT COUNT(*) FROM "{physical}"'), 1)
        row = self.conn.execute(
            'SELECT * FROM "__lsm_table" WHERE "table_name" = ?', (physical,)
        ).fetchone()
        self.assertEqual(row["status"], "trashed")
        self.assertEqual(row["trashed_op"], op)
        self.assertEqual(
            [f.name for f in get_fields(self.conn, physical)], ["金额", "备注"]
        )
        self.assertEqual(
            self.count('SELECT "table_name" FROM "__lsm_view"'), physical
        )

    def test_drop_records_audit(self):
        op = drop_table(self.conn, "订单")
        record = get_op(self.conn, op)
        self.assertEqual(record["kind"], "drop_table")
        self.assertEqual(record["table"], "订单")
        self.assertEqual(record["source"], "web")
        self.assertEqual(record["irreversible"], 0)

    def test_drop_rejects_unknown_table(self):
        with self.assertRaises(ValueError):
            drop_table(self.conn, "不存在")

    def test_operations_join_outer_transaction(self):
        self.conn.execute("BEGIN")
        drop_table(self.conn, "订单")
        self.conn.rollback()
        self.assertIn("订单", list_tables(self.conn))
        self.assertEqual(list_trashed_tables(self.conn), [])
        self.assertEqual(self.count('SELECT COUNT(*) FROM "__lsm_op"'), 0)

    def test_restore_brings_table_back(self):
        drop_table(self.conn, "订单")
        op = restore_table(self.conn, "订单")
        self.assertIn("订单", list_tables(self.conn))
        self.assertEqual(list_trashed_tables(self.conn), [])
        self.assertEqual(self.count('SELECT "金额" FROM "订单"'), 100)
        self.assertEqual([f.name for f in get_fields(self.conn, "订单")], ["金额", "备注"])
        row = self.conn.execute(
            'SELECT * FROM "__lsm_table" WHERE "table_name" = ?', ("订单",)
        ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["trashed_op"])
        self.assertEqual(get_op(self.conn, op)["kind"], "restore_table")

    def test_restore_avoids_name_clash(self):
        drop_table(self.conn, "订单")
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        restore_table(self.conn, "订单")
        self.assertIn("订单_2", list_tables(self.conn))
        self.assertEqual(self.count('SELECT "金额" FROM "订单_2"'), 100)
        self.assertEqual([f.name for f in get_fields(self.conn, "订单")], ["金额"])

    def test_restore_adds_suffix_until_free(self):
        drop_table(self.conn, "订单")
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        create_table(self.conn, "订单_2", [ColumnSpec("金额", "number")])
        restore_table(self.conn, "订单")
        self.assertIn("订单_3", list_tables(self.conn))

    def test_restore_rejects_unknown_table(self):
        with self.assertRaises(ValueError):
            restore_table(self.conn, "不存在")

    def test_rename_moves_table_and_metadata(self):
        op = rename_table(self.conn, "订单", "发票")
        self.assertEqual(list_tables(self.conn), ["发票"])
        self.assertEqual([f.name for f in get_fields(self.conn, "发票")], ["金额", "备注"])
        self.assertEqual(get_fields(self.conn, "订单"), [])
        self.assertEqual(self.count('SELECT COUNT(*) FROM "发票"'), 1)
        self.assertEqual(self.count('SELECT "table_name" FROM "__lsm_view"'), "发票")
        indexes = sorted(
            r["name"] for r in self.conn.execute('PRAGMA index_list("发票")')
        )
        self.assertEqual(
            sorted(indexes), sorted(["__lsm_idx_2_发票_2_金额", "__lsm_idx_2_发票_2_备注"])
        )
        record = get_op(self.conn, op)
        self.assertEqual(record["kind"], "rename_table")
        self.assertEqual(record["payload"], {"old": "订单", "new": "发票"})

    def test_rename_rejects_existing_target(self):
        create_table(self.conn, "客户", [ColumnSpec("名称", "text")])
        with self.assertRaises(ValueError):
            rename_table(self.conn, "订单", "客户")

    def test_rename_rejects_internal_name(self):
        with self.assertRaises(ValueError):
            rename_table(self.conn, "订单", "__lsm_hidden")

    def test_rename_rejects_unknown_table(self):
        with self.assertRaises(ValueError):
            rename_table(self.conn, "不存在", "新表")

    def test_create_after_drop_creates_indexes_again(self):
        # 回收站里的表若继续占着索引名，新表会静默没有索引。
        drop_table(self.conn, "订单")
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        indexes = [r["name"] for r in self.conn.execute('PRAGMA index_list("订单")')]
        self.assertIn("__lsm_idx_2_订单_2_金额", indexes)

    def test_list_trashed_tables_shape(self):
        op = drop_table(self.conn, "订单")
        self.assertEqual(
            list_trashed_tables(self.conn),
            [{"name": "订单", "table_name": f"__lsm_trash_{op}_订单", "trashed_op": op}],
        )

    def test_purge_drops_physical_table_and_metadata(self):
        trashed_op = drop_table(self.conn, "订单")
        physical = f"__lsm_trash_{trashed_op}_订单"
        op = purge_table(self.conn, "订单")
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (physical,),
            ).fetchone()
        )
        for meta in ("__lsm_table", "__lsm_field", "__lsm_view"):
            self.assertEqual(self.count(f'SELECT COUNT(*) FROM "{meta}"'), 0)
        self.assertEqual(list_trashed_tables(self.conn), [])
        record = get_op(self.conn, op)
        self.assertEqual(record["kind"], "purge_table")
        self.assertEqual(record["table"], "订单")
        self.assertEqual(record["irreversible"], 1)

    def test_purge_accepts_physical_name(self):
        trashed_op = drop_table(self.conn, "订单")
        physical = f"__lsm_trash_{trashed_op}_订单"
        purge_table(self.conn, physical)
        self.assertEqual(list_trashed_tables(self.conn), [])

    def test_drop_external_table_still_enters_trash(self):
        self.conn.execute('CREATE TABLE "外部" ("a" TEXT)')
        op = drop_table(self.conn, "外部")
        self.assertEqual(
            list_trashed_tables(self.conn),
            [
                {
                    "name": "外部",
                    "table_name": f"__lsm_trash_{op}_外部",
                    "trashed_op": op,
                }
            ],
        )
        restore_table(self.conn, "外部")
        self.assertIn("外部", list_tables(self.conn))

    def test_rename_rejects_blank_name(self):
        with self.assertRaises(ValueError):
            rename_table(self.conn, "订单", "  ")

    def test_rename_rejects_case_only_collision(self):
        create_table(self.conn, "abc", [ColumnSpec("a", "text")])
        with self.assertRaises(ValueError):
            rename_table(self.conn, "abc", "ABC")

    def test_rename_rejects_view_name(self):
        self.conn.execute('CREATE VIEW "报表" AS SELECT 1 AS "x"')
        with self.assertRaises(ValueError):
            rename_table(self.conn, "订单", "报表")

    def test_rename_strips_new_name(self):
        rename_table(self.conn, "订单", "  发票  ")
        self.assertIn("发票", list_tables(self.conn))

    def test_restore_rejects_ghost_trash_entry(self):
        op = drop_table(self.conn, "订单")
        self.conn.execute(f'DROP TABLE "__lsm_trash_{op}_订单"')
        with self.assertRaises(ValueError):
            restore_table(self.conn, "订单")

    def test_purge_rejects_ghost_trash_entry(self):
        op = drop_table(self.conn, "订单")
        self.conn.execute(f'DROP TABLE "__lsm_trash_{op}_订单"')
        with self.assertRaises(ValueError):
            purge_table(self.conn, "订单")

    def test_purge_rejects_active_table(self):
        with self.assertRaises(ValueError):
            purge_table(self.conn, "订单")


if __name__ == "__main__":
    unittest.main()
