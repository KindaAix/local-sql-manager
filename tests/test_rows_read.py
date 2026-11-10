import sqlite3
import unittest

from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.models import ViewConfig
from local_sql_manager.services.rows import _has_rowid, get_page
from local_sql_manager.services.schema import ColumnSpec, create_table
from local_sql_manager.services.util import utcnow_iso


class TestGetPage(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        create_table(
            self.conn,
            "订单",
            [ColumnSpec("状态", "select", ("新建", "完成")), ColumnSpec("金额", "number")],
        )

    def tearDown(self):
        self.conn.close()

    def insert(self, status, amount, deleted_at=None):
        now = utcnow_iso()
        self.conn.execute(
            'INSERT INTO "订单" ("created_at", "updated_at", "deleted_at", "状态", "金额") '
            "VALUES (?, ?, ?, ?, ?)",
            (now, now, deleted_at, status, amount),
        )

    def test_excludes_soft_deleted_and_paginates(self):
        self.insert("完成", 10)
        self.insert("完成", 20, deleted_at=utcnow_iso())
        self.insert("新建", 30)
        page = get_page(self.conn, "订单", ViewConfig(), page=1, page_size=1)
        self.assertEqual(len(page.rows), 1)
        self.assertEqual(page.total, 2)
        self.assertEqual(page.page, 1)
        self.assertEqual(page.page_size, 1)

    def test_second_page_skips_first_row(self):
        for amount in (10, 20, 30):
            self.insert("完成", amount)
        page = get_page(self.conn, "订单", ViewConfig(), page=2, page_size=2)
        self.assertEqual(page.total, 3)
        self.assertEqual([row["金额"] for row in page.rows], [30])

    def test_rows_are_dicts_with_aux_columns(self):
        self.insert("完成", 10)
        page = get_page(self.conn, "订单", ViewConfig())
        row = page.rows[0]
        self.assertIsInstance(row, dict)
        self.assertEqual(row["id"], 1)
        self.assertIn("created_at", row)
        self.assertIn("状态", row)

    def test_filters_apply_to_rows_and_total(self):
        self.insert("完成", 10)
        self.insert("完成", 20)
        self.insert("新建", 30)
        view = ViewConfig(
            filters=[
                {"column": "状态", "op": "eq", "value": "完成"},
                {"column": "金额", "op": "gte", "value": 20},
            ],
            sorts=[{"column": "金额", "dir": "desc"}],
        )
        page = get_page(self.conn, "订单", view)
        self.assertEqual(page.total, 1)
        self.assertEqual([row["金额"] for row in page.rows], [20])

    def test_group_counts_whole_filter_set(self):
        for amount in (10, 20, 30, 40):
            self.insert("完成", amount)
        self.insert("新建", 5)
        self.insert("新建", 15)
        view = ViewConfig(group={"column": "状态"})
        page = get_page(self.conn, "订单", view, page=1, page_size=2)
        self.assertEqual(len(page.rows), 2)
        counts = {g.key: g.count for g in page.groups}
        self.assertEqual(counts, {"完成": 4, "新建": 2})

    def test_group_aggregates_cover_whole_set(self):
        for amount in (10, 20, 30, 40):
            self.insert("完成", amount)
        self.insert("新建", 5)
        view = ViewConfig(
            group={"column": "状态"},
            aggregates=[{"column": "金额", "fn": "sum"}],
        )
        page = get_page(self.conn, "订单", view, page=1, page_size=1)
        sums = {g.key: g.aggregates[0] for g in page.groups}
        self.assertEqual(sums["完成"], 100.0)
        self.assertEqual(sums["新建"], 5.0)

    def test_group_without_aggregates(self):
        self.insert("完成", 10)
        page = get_page(
            self.conn, "订单", ViewConfig(group={"column": "状态"})
        )
        self.assertEqual(page.groups[0].aggregates, ())

    def test_unknown_group_column_ignored(self):
        self.insert("完成", 10)
        page = get_page(
            self.conn, "订单", ViewConfig(group={"column": "没有"})
        )
        self.assertEqual(page.groups, ())
        self.assertEqual(page.total, 1)

    def test_page_beyond_last_is_empty(self):
        self.insert("完成", 10)
        page = get_page(self.conn, "订单", ViewConfig(), page=5, page_size=100)
        self.assertEqual(page.rows, [])
        self.assertEqual(page.total, 1)

    def test_table_without_soft_delete_column(self):
        self.conn.execute('CREATE TABLE "legacy" ("id" INTEGER PRIMARY KEY, "名称" TEXT)')
        self.conn.execute("INSERT INTO legacy (\"名称\") VALUES ('a')")
        page = get_page(self.conn, "legacy", ViewConfig())
        self.assertEqual(page.total, 1)
        self.assertEqual(page.rows[0]["名称"], "a")

    def test_without_rowid_table_pages_without_order(self):
        self.conn.execute(
            'CREATE TABLE "noid2" ("k" TEXT PRIMARY KEY) WITHOUT ROWID'
        )
        self.conn.executemany(
            'INSERT INTO "noid2" ("k") VALUES (?)', [("a",), ("b",)]
        )
        page = get_page(self.conn, "noid2", ViewConfig())
        self.assertEqual(page.total, 2)
        self.assertEqual(sorted(row["k"] for row in page.rows), ["a", "b"])

    def test_rowid_detection_ignores_sql_comment(self):
        self.conn.execute(
            'CREATE TABLE "c1" ("a" TEXT /* WITHOUT ROWID */)'
        )
        self.assertTrue(_has_rowid(self.conn, "c1"))
        self.conn.execute('INSERT INTO "c1" ("a") VALUES (?)', ("x",))
        page = get_page(self.conn, "c1", ViewConfig())
        self.assertEqual(page.rows[0]["a"], "x")

    def test_rowid_detection_for_without_rowid_and_view(self):
        self.conn.execute(
            'CREATE TABLE "w1" ("k" TEXT PRIMARY KEY) WITHOUT ROWID'
        )
        self.conn.execute('CREATE VIEW "v1" AS SELECT 1 AS "x"')
        self.assertFalse(_has_rowid(self.conn, "w1"))
        self.assertFalse(_has_rowid(self.conn, "v1"))

    def test_without_rowid_pages_by_primary_key(self):
        self.conn.execute(
            'CREATE TABLE "noid3" ("k" TEXT PRIMARY KEY, "v" TEXT) WITHOUT ROWID'
        )
        self.conn.executemany(
            'INSERT INTO "noid3" ("k", "v") VALUES (?, ?)',
            [("c", "3"), ("a", "1"), ("b", "2")],
        )
        first = get_page(self.conn, "noid3", ViewConfig(), page=1, page_size=2)
        second = get_page(self.conn, "noid3", ViewConfig(), page=2, page_size=2)
        self.assertEqual([row["k"] for row in first.rows], ["a", "b"])
        self.assertEqual([row["k"] for row in second.rows], ["c"])

    def test_bad_page_falls_back_to_one(self):
        self.insert("完成", 10)
        page = get_page(self.conn, "订单", ViewConfig(), page="abc")
        self.assertEqual(page.page, 1)
        self.assertEqual(len(page.rows), 1)

    def test_page_size_is_capped(self):
        self.insert("完成", 10)
        page = get_page(self.conn, "订单", ViewConfig(), page_size=5000)
        self.assertEqual(page.page_size, 1000)

    def test_table_without_id_pages_by_rowid(self):
        self.conn.execute('CREATE TABLE "noid" ("名称" TEXT)')
        self.conn.executemany(
            'INSERT INTO "noid" ("名称") VALUES (?)', [("a",), ("b",), ("c",)]
        )
        first = get_page(self.conn, "noid", ViewConfig(), page=1, page_size=2)
        second = get_page(self.conn, "noid", ViewConfig(), page=2, page_size=2)
        names = [row["名称"] for row in first.rows] + [
            row["名称"] for row in second.rows
        ]
        self.assertEqual(len(first.rows) + len(second.rows), 3)
        self.assertFalse(
            {row["名称"] for row in first.rows} & {row["名称"] for row in second.rows}
        )
        self.assertEqual(names, ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
