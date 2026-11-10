import os
import sqlite3
import tempfile
import unittest

from local_sql_manager.services import merge, undo
from local_sql_manager.services.audit import get_op, record
from local_sql_manager.services.library import ensure_meta, open_library
from local_sql_manager.services.models import StaleOperation
from local_sql_manager.services.rows import insert_rows
from local_sql_manager.services.schema import (
    ColumnSpec,
    create_table,
    drop_table,
    get_fields,
    list_tables,
    list_trashed_tables,
)
from local_sql_manager.services.util import utcnow_iso


def _quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def add_table(conn, name, specs, rows):
    """建一张受管表并写入若干行；specs 是 [(列名, 逻辑类型)]。"""
    create_table(conn, name, [ColumnSpec(column, kind) for column, kind in specs])
    now = utcnow_iso()
    columns = ["created_at", "updated_at"] + [column for column, _ in specs]
    sql = (
        "INSERT INTO " + _quote(name)
        + " (" + ", ".join(_quote(column) for column in columns) + ") VALUES ("
        + ", ".join("?" for _ in columns) + ")"
    )
    for values in rows:
        conn.execute(sql, [now, now, *values])


class MergeTestBase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        self.make("一月", [("金额", "number"), ("备注", "text")], [(100, "首"), (200, None)])
        self.make("二月", [("金额", "number"), ("备注", "text")], [(300, "三"), (400, "四")])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def make(self, name, specs, rows):
        add_table(self.conn, name, specs, rows)

    def column(self, table, column):
        rows = self.conn.execute(
            f'SELECT {_quote(column)} FROM {_quote(table)} ORDER BY "id"'
        ).fetchall()
        return [row[0] for row in rows]

    def count(self, table):
        return self.conn.execute(
            f'SELECT COUNT(*) FROM {_quote(table)}'
        ).fetchone()[0]


class TestPlanMerge(MergeTestBase):
    def test_aligns_case_insensitively(self):
        self.make("英文", [(" Amount ", "number")], [(1,)])
        self.make("大写", [("amount", "number")], [(2,)])
        plan = merge.plan_merge(self.conn, ["英文", "大写"], "合计")
        self.assertEqual(plan["columns"], ["Amount"])

    def test_missing_columns_are_not_conflicts(self):
        self.make("缺列", [("金额", "number")], [(9,)])
        plan = merge.plan_merge(self.conn, ["一月", "缺列"], "合计")
        self.assertEqual(plan["columns"], ["金额", "备注"])
        self.assertEqual(plan["conflicts"], [])

    def test_type_conflict_is_reported(self):
        self.make("文本表", [("值", "text")], [("abc",)])
        self.make("数字表", [("值", "number")], [(42,)])
        plan = merge.plan_merge(self.conn, ["文本表", "数字表"], "合计")
        self.assertEqual(
            plan["conflicts"],
            [{"column": "值", "types": {"文本表": "text", "数字表": "number"}}],
        )
        self.assertEqual(plan["columns"], ["值"])

    def test_counts_rows_and_sources(self):
        plan = merge.plan_merge(self.conn, ["一月", "二月"], "合计")
        self.assertEqual(plan["rows"], 4)
        self.assertEqual(
            plan["sources"],
            [{"table": "一月", "rows": 2}, {"table": "二月", "rows": 2}],
        )

    def test_soft_deleted_rows_are_not_counted(self):
        self.conn.execute(
            'UPDATE "一月" SET "deleted_at" = ? WHERE "金额" = ?',
            (utcnow_iso(), 100),
        )
        plan = merge.plan_merge(self.conn, ["一月", "二月"], "合计")
        self.assertEqual(plan["rows"], 3)
        self.assertEqual(plan["sources"][0]["rows"], 1)

    def test_target_exists_rejected(self):
        with self.assertRaises(ValueError) as caught:
            merge.plan_merge(self.conn, ["一月", "二月"], "一月")
        self.assertIn("目标表已存在", str(caught.exception))

    def test_needs_two_tables(self):
        with self.assertRaises(ValueError):
            merge.plan_merge(self.conn, ["一月"], "合计")

    def test_unregistered_table_rejected(self):
        self.conn.execute('CREATE TABLE "外部" ("x" TEXT)')
        with self.assertRaises(ValueError) as caught:
            merge.plan_merge(self.conn, ["外部", "一月"], "合计")
        self.assertIn("未登记", str(caught.exception))

    def test_trashed_table_rejected(self):
        drop_table(self.conn, "一月")
        with self.assertRaises(ValueError):
            merge.plan_merge(self.conn, ["一月", "二月"], "合计")

    def test_plan_writes_nothing(self):
        before = self.conn.execute('SELECT COUNT(*) FROM "__lsm_op"').fetchone()[0]
        merge.plan_merge(self.conn, ["一月", "二月"], "合计")
        after = self.conn.execute('SELECT COUNT(*) FROM "__lsm_op"').fetchone()[0]
        self.assertEqual(before, after)
        self.assertNotIn("合计", list_tables(self.conn))


class TestMergeTables(MergeTestBase):
    def test_merges_rows_and_values(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        self.assertIsInstance(op, int)
        self.assertEqual(self.count("合计"), 4)
        self.assertEqual(sorted(self.column("合计", "金额")), [100, 200, 300, 400])
        self.assertEqual(
            [field.name for field in get_fields(self.conn, "合计")], ["金额", "备注"]
        )
        self.assertIn("合计", list_tables(self.conn))
        # 源表原样保留
        self.assertEqual(self.count("一月"), 2)
        self.assertEqual(self.count("二月"), 2)

    def test_aux_columns_are_filled(self):
        merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        rows = self.conn.execute(
            'SELECT "id", "created_at", "updated_at", "deleted_at" FROM "合计"'
        ).fetchall()
        self.assertEqual([row["id"] for row in rows], [1, 2, 3, 4])
        for row in rows:
            self.assertTrue(row["created_at"])
            self.assertTrue(row["updated_at"])
            self.assertIsNone(row["deleted_at"])

    def test_missing_column_filled_with_null(self):
        self.make("缺列", [("金额", "number")], [(500,)])
        merge.merge_tables(self.conn, ["一月", "缺列"], "合计")
        self.assertEqual(self.count("合计"), 3)
        self.assertIsNone(self.column("合计", "备注")[-1])
        self.assertEqual(self.column("合计", "金额"), [100, 200, 500])

    def test_case_and_spaces_aligned_in_data(self):
        self.make("英文", [(" Amount ", "number")], [(1,)])
        self.make("大写", [("amount", "number")], [(2,)])
        merge.merge_tables(self.conn, ["英文", "大写"], "合计")
        self.assertEqual(self.column("合计", "Amount"), [1, 2])

    def test_type_conflict_still_merges(self):
        self.make("文本表", [("值", "text")], [("abc",)])
        self.make("数字表", [("值", "number")], [(42,)])
        merge.merge_tables(self.conn, ["文本表", "数字表"], "合计")
        self.assertEqual(self.count("合计"), 2)
        values = sorted(str(value) for value in self.column("合计", "值"))
        self.assertEqual(values, ["42.0", "abc"])

    def test_soft_deleted_rows_not_copied(self):
        self.conn.execute(
            'UPDATE "一月" SET "deleted_at" = ? WHERE "金额" = ?',
            (utcnow_iso(), 100),
        )
        merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        self.assertEqual(self.count("合计"), 3)
        self.assertNotIn(100, self.column("合计", "金额"))

    def test_target_exists_rejected(self):
        with self.assertRaises(ValueError) as caught:
            merge.merge_tables(self.conn, ["一月", "二月"], "二月")
        self.assertIn("目标表已存在", str(caught.exception))
        self.assertEqual(self.count("二月"), 2)

    def test_single_table_rejected(self):
        with self.assertRaises(ValueError):
            merge.merge_tables(self.conn, ["一月"], "合计")

    def test_unregistered_table_rejected(self):
        self.conn.execute('CREATE TABLE "外部" ("x" TEXT)')
        with self.assertRaises(ValueError):
            merge.merge_tables(self.conn, ["外部", "一月"], "合计")
        self.assertNotIn("合计", list_tables(self.conn))

    def test_no_business_columns(self):
        self.make("空A", [], [(), ()])
        self.make("空B", [], [()])
        merge.merge_tables(self.conn, ["空A", "空B"], "合计")
        self.assertEqual(self.count("合计"), 3)
        self.assertEqual(get_fields(self.conn, "合计"), [])

    def test_records_merge_audit(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        entry = get_op(self.conn, op)
        self.assertEqual(entry["kind"], "merge")
        self.assertEqual(entry["table"], "合计")
        self.assertEqual(entry["source"], "web")
        self.assertEqual(entry["irreversible"], 0)
        self.assertIsNone(entry["undone_by"])
        self.assertEqual(entry["payload"]["target"], "合计")
        self.assertEqual(entry["payload"]["tables"], ["一月", "二月"])
        self.assertEqual(entry["payload"]["columns"], ["金额", "备注"])
        self.assertEqual(entry["payload"]["conflicts"], [])
        self.assertEqual(entry["payload"]["rows"], 4)

    def test_source_argument_goes_to_audit(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计", source="mcp")
        self.assertEqual(get_op(self.conn, op)["source"], "mcp")


class TestMergeTransaction(MergeTestBase):
    def test_merge_does_not_commit(self):
        self.conn.close()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "库.sqlite")
            library = open_library(path)
            conn = library.conn
            try:
                add_table(conn, "一月", [("金额", "number")], [(1,)])
                add_table(conn, "二月", [("金额", "number")], [(2,)])
                conn.commit()
                merge.merge_tables(conn, ["一月", "二月"], "合计")
                self.assertTrue(conn.in_transaction)

                other = sqlite3.connect(path)
                try:
                    names = [
                        row[0]
                        for row in other.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    ]
                    self.assertNotIn("合计", names)
                finally:
                    other.close()

                conn.commit()
                other = sqlite3.connect(path)
                try:
                    names = [
                        row[0]
                        for row in other.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    ]
                    self.assertIn("合计", names)
                    self.assertEqual(
                        other.execute('SELECT COUNT(*) FROM "合计"').fetchone()[0], 2
                    )
                finally:
                    other.close()
            finally:
                conn.close()

    def test_rollback_discards_merge(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        self.conn.rollback()
        self.assertNotIn("合计", list_tables(self.conn))
        self.assertIsNone(get_op(self.conn, op))

    def test_merge_joins_outer_transaction(self):
        self.conn.execute("BEGIN")
        merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        self.conn.rollback()
        self.assertNotIn("合计", list_tables(self.conn))


class TestMergeMapping(MergeTestBase):
    def setUp(self):
        super().setUp()
        self.make("英文", [("Amount", "number"), ("Note", "text")], [(5, "five")])
        self.make("中文", [("金额", "number"), ("备注", "text")], [(6, "六")])

    def test_mapping_aligns_different_names(self):
        mapping = {"金额": {"中文": "金额", "英文": "Amount"}}
        plan = merge.plan_merge(self.conn, ["中文", "英文"], "合计", mapping)
        self.assertEqual(plan["columns"], ["金额"])
        merge.merge_tables(self.conn, ["中文", "英文"], "合计", mapping)
        self.assertEqual(self.column("合计", "金额"), [6, 5])

    def test_mapping_controls_column_order(self):
        mapping = {
            "备注": {"英文": "Note"},
            "金额": {"英文": "Amount"},
        }
        plan = merge.plan_merge(self.conn, ["英文", "中文"], "合计", mapping)
        self.assertEqual(plan["columns"], ["备注", "金额"])
        merge.merge_tables(self.conn, ["英文", "中文"], "合计", mapping)
        self.assertEqual(
            [field.name for field in get_fields(self.conn, "合计")], ["备注", "金额"]
        )
        # 中文表没有映射备注列，补 NULL
        self.assertEqual(self.column("合计", "备注"), ["five", None])

    def test_mapping_values_ignored_if_unmapped(self):
        mapping = {"金额": {"中文": "金额", "英文": "Amount"}}
        merge.merge_tables(self.conn, ["中文", "英文"], "合计", mapping)
        self.assertNotIn("备注", [field.name for field in get_fields(self.conn, "合计")])

    def test_mapping_unknown_source_column_rejected(self):
        mapping = {"金额": {"中文": "不存在"}}
        with self.assertRaises(ValueError):
            merge.plan_merge(self.conn, ["中文", "英文"], "合计", mapping)

    def test_mapping_unknown_table_rejected(self):
        mapping = {"金额": {"外部": "金额"}}
        with self.assertRaises(ValueError):
            merge.plan_merge(self.conn, ["中文", "英文"], "合计", mapping)

    def test_mapping_target_type_from_first_table(self):
        self.make("文本", [("Amount", "text")], [("7",)])
        mapping = {"金额": {"文本": "Amount", "中文": "金额"}}
        plan = merge.plan_merge(self.conn, ["文本", "中文"], "合计", mapping)
        self.assertEqual(
            plan["conflicts"],
            [{"column": "金额", "types": {"文本": "text", "中文": "number"}}],
        )
        merge.merge_tables(self.conn, ["文本", "中文"], "合计", mapping)
        self.assertEqual(self.count("合计"), 2)


class TestMergeUndo(MergeTestBase):
    def test_undo_moves_target_to_trash(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        undo_id = undo.undo(self.conn, op)

        self.assertNotIn("合计", list_tables(self.conn))
        trashed = list_trashed_tables(self.conn)
        self.assertEqual([item["name"] for item in trashed], ["合计"])
        self.assertEqual(trashed[0]["trashed_op"], op)
        physical = trashed[0]["table_name"]
        self.assertEqual(self.count(physical), 4)

        self.assertEqual(get_op(self.conn, undo_id)["kind"], "undo")
        self.assertEqual(get_op(self.conn, op)["undone_by"], undo_id)
        # 源表不受影响
        self.assertEqual(self.count("一月"), 2)
        self.assertEqual(self.count("二月"), 2)

    def test_undo_twice_restores_table(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        undo_id = undo.undo(self.conn, op)
        undo.undo(self.conn, undo_id)
        self.assertIn("合计", list_tables(self.conn))
        self.assertEqual(list_trashed_tables(self.conn), [])
        self.assertEqual(self.count("合计"), 4)

    def test_undo_refuses_after_later_write(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        insert_rows(self.conn, "合计", [{"金额": 500}])
        self.conn.commit()

        with self.assertRaises(StaleOperation):
            undo.undo(self.conn, op)
        self.assertIn("合计", list_tables(self.conn))
        self.assertEqual(self.count("合计"), 5)

    def test_undo_refuses_after_later_undo_of_that_write(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        write_op = insert_rows(self.conn, "合计", [{"金额": 500}])
        self.conn.commit()
        undo.undo(self.conn, write_op)
        self.conn.commit()
        # 后续写入已被撤销，不再阻塞合并的撤销
        undo.undo(self.conn, op)
        self.assertNotIn("合计", list_tables(self.conn))

    def test_view_op_does_not_block_undo(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        record(self.conn, "view", "合计", "web", {"name": "全部记录"})
        self.conn.commit()
        undo.undo(self.conn, op)
        self.assertNotIn("合计", list_tables(self.conn))

    def test_undo_on_missing_target_raises_stale(self):
        op = merge.merge_tables(self.conn, ["一月", "二月"], "合计")
        self.conn.execute('ALTER TABLE "合计" RENAME TO "改名了"')
        self.conn.commit()
        with self.assertRaises(StaleOperation):
            undo.undo(self.conn, op)


if __name__ == "__main__":
    unittest.main()
