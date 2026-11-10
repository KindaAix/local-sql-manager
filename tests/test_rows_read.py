import sqlite3
import unittest

from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.models import ViewConfig
from local_sql_manager.services.rows import get_page
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

    def test_table_without_id_pages_by_rowid(self):
        self.conn.execute('CREATE TABLE "noid" ("名称" TEXT)')
        self.conn.executemany(
            'INSERT INTO "noid" ("名称") VALUES (?)', [("a",), ("b",), ("c",)]
        )
        names = []
        for number in (1, 2, 3):
            page = get_page(self.conn, "noid", ViewConfig(), page=number, page_size=1)
            names.extend(row["名称"] for row in page.rows)
        self.assertEqual(names, ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
