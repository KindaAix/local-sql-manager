"""操作级撤销的服务层测试。"""

import sqlite3
import unittest

from local_sql_manager.services import audit
from local_sql_manager.services.audit import get_op
from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.models import Irreversible, StaleOperation
from local_sql_manager.services.rows import (
    insert_rows,
    purge_rows,
    restore_rows,
    soft_delete,
    update_cells,
)
from local_sql_manager.services.schema import (
    ColumnSpec,
    create_table,
    drop_table,
    list_tables,
    list_trashed_tables,
    rename_table,
    restore_table,
)
from local_sql_manager.services import undo as undo_service

OLD_TS = "2020-01-01T00:00:00Z"


class BaseUndoTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        create_table(
            self.conn,
            "订单",
            [
                ColumnSpec("状态", "select", ("新建", "完成")),
                ColumnSpec("金额", "number"),
            ],
        )
        self.first = self.seed("完成", 100)
        self.second = self.seed("新建", 50)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def seed(self, status, amount):
        cur = self.conn.execute(
            'INSERT INTO "订单" ("created_at", "updated_at", "状态", "金额")'
            " VALUES (?, ?, ?, ?)",
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

    def count(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]

    def swap_handler(self, kind, handler):
        """临时替换某 kind 的处理器，测试结束后恢复原状。

        层 6 起 import/merge/create_field 等已有模块级真实处理器，直接
        unregister 会把它们永久摘掉、污染后续用例。
        """
        previous = undo_service._HANDLERS.get(kind)
        undo_service.register(kind, handler)

        def restore():
            if previous is None:
                undo_service.unregister(kind)
            else:
                undo_service.register(kind, previous)

        self.addCleanup(restore)


class TestUndoRows(BaseUndoTestCase):
    def test_undo_update_restores_previous_value(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        undo_service.undo(self.conn, op)
        self.assertEqual(self.cell(self.first, "金额"), 100)

    def test_undo_records_undo_op_and_marks_original(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        undo_id = undo_service.undo(self.conn, op)
        record = self.op(undo_id)
        self.assertEqual(record["kind"], "undo")
        self.assertEqual(record["table"], "订单")
        self.assertEqual(record["payload"]["op"], op)
        self.assertEqual(record["payload"]["rows"][0]["id"], self.first)
        self.assertEqual(self.op(op)["undone_by"], undo_id)

    def test_undo_of_undo_reapplies_the_change(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        first_undo = undo_service.undo(self.conn, op)
        second_undo = undo_service.undo(self.conn, first_undo)
        self.assertEqual(self.cell(self.first, "金额"), 500)
        self.assertEqual(self.op(second_undo)["kind"], "undo")
        self.assertEqual(self.op(first_undo)["undone_by"], second_undo)

    def test_undo_update_is_stale_when_row_changed_after(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 600}},
            {self.first: self.updated_at(self.first)},
        )
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertIsNone(self.op(op)["undone_by"])
        self.assertEqual(self.cell(self.first, "金额"), 600)

    def test_undo_update_is_stale_when_other_column_changed_after(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        update_cells(
            self.conn,
            "订单",
            {self.first: {"状态": "新建"}},
            {self.first: self.updated_at(self.first)},
        )
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertEqual(self.cell(self.first, "金额"), 500)

    def test_undo_update_unblocks_after_later_op_is_undone(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        later = update_cells(
            self.conn,
            "订单",
            {self.first: {"状态": "新建"}},
            {self.first: self.updated_at(self.first)},
        )
        undo_service.undo(self.conn, later)
        undo_service.undo(self.conn, op)
        self.assertEqual(self.cell(self.first, "金额"), 100)
        self.assertEqual(self.cell(self.first, "状态"), "完成")

    def test_undo_update_keeps_earlier_delete_restore_undoable(self):
        delete_op = soft_delete(self.conn, "订单", [self.first])
        restore_op = restore_rows(self.conn, "订单", [self.first])
        restored_at = self.updated_at(self.first)
        update_op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: restored_at},
        )

        undo_service.undo(self.conn, update_op)
        self.assertEqual(self.cell(self.first, "金额"), 100)
        self.assertEqual(self.updated_at(self.first), restored_at)

        undo_service.undo(self.conn, restore_op)
        self.assertIsNotNone(self.row(self.first)["deleted_at"])

        undo_service.undo(self.conn, delete_op)
        self.assertIsNone(self.row(self.first)["deleted_at"])

    def test_undo_batch_rolls_back_when_one_row_stale(self):
        op = audit.record(
            self.conn,
            "update",
            "订单",
            "web",
            {
                "rows": [
                    {"id": self.first, "before": {"金额": 100}, "after": {"金额": 500}},
                    {"id": self.second, "before": {"金额": 50}, "after": {"金额": 60}},
                ]
            },
        )
        self.conn.execute(
            'UPDATE "订单" SET "金额" = 500 WHERE "id" = ?', (self.first,)
        )
        self.conn.execute(
            'UPDATE "订单" SET "金额" = 61 WHERE "id" = ?', (self.second,)
        )
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertEqual(self.cell(self.first, "金额"), 500)
        self.assertEqual(self.cell(self.second, "金额"), 61)

    def test_failed_undo_leaves_no_audit_rows(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 600}},
            {self.first: self.updated_at(self.first)},
        )
        before = self.count('SELECT COUNT(*) FROM "__lsm_op"')
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertEqual(self.count('SELECT COUNT(*) FROM "__lsm_op"'), before)

    def test_undo_accepts_string_op_id(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        undo_service.undo(self.conn, str(op))
        self.assertEqual(self.cell(self.first, "金额"), 100)

    def test_undo_update_is_stale_when_row_missing(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        self.conn.execute('DELETE FROM "订单" WHERE "id" = ?', (self.first,))
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_undo_update_touches_only_recorded_rows(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        update_cells(
            self.conn,
            "订单",
            {self.second: {"金额": 777}},
            {self.second: self.updated_at(self.second)},
        )
        undo_service.undo(self.conn, op)
        self.assertEqual(self.cell(self.first, "金额"), 100)
        self.assertEqual(self.cell(self.second, "金额"), 777)

    def test_undo_insert_removes_only_recorded_row(self):
        op = insert_rows(self.conn, "订单", [{"状态": "新建", "金额": 10}])
        new_id = self.count('SELECT MAX("id") FROM "订单"')
        undo_service.undo(self.conn, op)
        self.assertIsNone(self.row(new_id))
        self.assertIsNotNone(self.row(self.first))

    def test_undo_insert_is_stale_when_row_changed_after(self):
        op = insert_rows(self.conn, "订单", [{"状态": "新建", "金额": 10}])
        new_id = self.count('SELECT MAX("id") FROM "订单"')
        update_cells(
            self.conn,
            "订单",
            {new_id: {"金额": 11}},
            {new_id: self.updated_at(new_id)},
        )
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertIsNotNone(self.row(new_id))

    def test_undo_insert_of_undo_restores_same_row(self):
        op = insert_rows(self.conn, "订单", [{"状态": "新建", "金额": 10}])
        new_id = self.count('SELECT MAX("id") FROM "订单"')
        first_undo = undo_service.undo(self.conn, op)
        undo_service.undo(self.conn, first_undo)
        restored = self.row(new_id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored["金额"], 10)
        self.assertIsNone(restored["deleted_at"])

    def test_undo_delete_restores_rows(self):
        op = soft_delete(self.conn, "订单", [self.first])
        undo_service.undo(self.conn, op)
        self.assertIsNone(self.row(self.first)["deleted_at"])

    def test_undo_delete_is_stale_after_purge(self):
        op = soft_delete(self.conn, "订单", [self.first])
        purge_rows(self.conn, "订单", [self.first])
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_undo_restore_soft_deletes_again(self):
        soft_delete(self.conn, "订单", [self.first])
        op = restore_rows(self.conn, "订单", [self.first])
        undo_service.undo(self.conn, op)
        self.assertIsNotNone(self.row(self.first)["deleted_at"])

    def test_undo_restores_blob_bytes(self):
        self.conn.execute('ALTER TABLE "订单" ADD COLUMN "附件" BLOB')
        self.conn.execute(
            'UPDATE "订单" SET "附件" = ? WHERE "id" = ?', (b"\x00old", self.first)
        )
        self.conn.commit()
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"附件": b"\x00new"}},
            {self.first: self.updated_at(self.first)},
        )
        undo_service.undo(self.conn, op)
        self.assertEqual(self.cell(self.first, "附件"), b"\x00old")

    def test_purge_is_irreversible(self):
        op = audit.record(self.conn, "purge", "订单", "web", {}, irreversible=True)
        with self.assertRaises(Irreversible):
            undo_service.undo(self.conn, op)

    def test_purge_without_flag_is_still_irreversible(self):
        op = audit.record(self.conn, "purge", "订单", "web", {})
        with self.assertRaises(Irreversible):
            undo_service.undo(self.conn, op)

    def test_purge_rows_is_irreversible(self):
        soft_delete(self.conn, "订单", [self.first])
        op = purge_rows(self.conn, "订单", [self.first])
        with self.assertRaises(Irreversible):
            undo_service.undo(self.conn, op)

    def test_undo_rejects_already_undone_op(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        undo_service.undo(self.conn, op)
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_unknown_kind_is_stale(self):
        op = audit.record(self.conn, "view", "订单", "web", {})
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_missing_op_raises_value_error(self):
        with self.assertRaises(ValueError):
            undo_service.undo(self.conn, 9999)

    def test_undo_source_is_recorded(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        undo_id = undo_service.undo(self.conn, op, source="mcp")
        self.assertEqual(self.op(undo_id)["source"], "mcp")

    def test_undo_does_not_commit(self):
        op = update_cells(
            self.conn,
            "订单",
            {self.first: {"金额": 500}},
            {self.first: self.updated_at(self.first)},
        )
        self.conn.commit()
        undo_service.undo(self.conn, op)
        self.conn.rollback()
        self.assertEqual(self.cell(self.first, "金额"), 500)
        self.assertIsNone(self.op(op)["undone_by"])


class TestUndoTables(BaseUndoTestCase):
    def test_undo_drop_table_restores_name_and_data(self):
        op = drop_table(self.conn, "订单")
        undo_service.undo(self.conn, op)
        self.assertIn("订单", list_tables(self.conn))
        self.assertEqual(self.count('SELECT COUNT(*) FROM "订单"'), 2)
        self.assertEqual(list_trashed_tables(self.conn), [])
        meta = self.conn.execute(
            'SELECT "status", "trashed_op" FROM "__lsm_table" WHERE "table_name" = ?',
            ("订单",),
        ).fetchone()
        self.assertEqual(meta["status"], "active")
        self.assertIsNone(meta["trashed_op"])

    def test_undo_drop_table_is_stale_when_name_taken(self):
        op = drop_table(self.conn, "订单")
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertEqual(len(list_trashed_tables(self.conn)), 1)

    def test_undo_create_table_moves_table_to_trash(self):
        op = audit.record(
            self.conn, "create_table", "客户", "web", {"name": "客户"}
        )
        create_table(self.conn, "客户", [ColumnSpec("名称", "text")])
        undo_service.undo(self.conn, op)
        self.assertNotIn("客户", list_tables(self.conn))
        self.assertIn("客户", [item["name"] for item in list_trashed_tables(self.conn)])

    def test_undo_create_table_is_stale_after_writes(self):
        op = audit.record(
            self.conn, "create_table", "客户", "web", {"name": "客户"}
        )
        create_table(self.conn, "客户", [ColumnSpec("名称", "text")])
        insert_rows(self.conn, "客户", [{"名称": "甲"}])
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertIn("客户", list_tables(self.conn))

    def test_undo_create_table_ignores_metadata_ops(self):
        op = audit.record(
            self.conn, "create_table", "客户", "web", {"name": "客户"}
        )
        create_table(self.conn, "客户", [ColumnSpec("名称", "text")])
        audit.record(
            self.conn,
            "view",
            "客户",
            "web",
            {"op": "create", "name": "全部记录", "config": {}},
        )
        undo_service.undo(self.conn, op)
        self.assertNotIn("客户", list_tables(self.conn))
        self.assertIn("客户", [item["name"] for item in list_trashed_tables(self.conn)])

    def test_field_op_blocks_create_undo_once_handler_registered(self):
        op = audit.record(
            self.conn, "create_table", "客户", "web", {"name": "客户"}
        )
        create_table(self.conn, "客户", [ColumnSpec("名称", "text")])
        audit.record(
            self.conn, "create_field", "客户", "web", {"column": "备注"}
        )

        # 层 6 起 fields 在服务包导入时就会注册处理器；这里临时覆盖，
        # 测试结束由 swap_handler 恢复真实处理器。
        self.swap_handler("create_field", lambda conn, op, source: {})
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_undo_rename_table_reverts_name(self):
        op = rename_table(self.conn, "订单", "发票")
        undo_service.undo(self.conn, op)
        self.assertIn("订单", list_tables(self.conn))
        self.assertNotIn("发票", list_tables(self.conn))
        self.assertEqual(self.count('SELECT COUNT(*) FROM "订单"'), 2)

    def test_undo_rename_table_is_stale_when_old_name_taken(self):
        op = rename_table(self.conn, "订单", "发票")
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertIn("发票", list_tables(self.conn))

    def test_undo_restore_table_trashes_it_again(self):
        drop_table(self.conn, "订单")
        op = restore_table(self.conn, "订单")
        undo_service.undo(self.conn, op)
        self.assertNotIn("订单", list_tables(self.conn))
        self.assertIn("订单", [item["name"] for item in list_trashed_tables(self.conn)])

    def test_undo_restore_table_with_suffix_trashes_current_name(self):
        drop_table(self.conn, "订单")
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        op = restore_table(self.conn, "订单")
        self.assertIn("订单_2", list_tables(self.conn))
        undo_service.undo(self.conn, op)
        self.assertNotIn("订单_2", list_tables(self.conn))
        self.assertIn("订单_2", [item["name"] for item in list_trashed_tables(self.conn)])

    def test_undo_chain_drop_restore_drop_restore(self):
        op = drop_table(self.conn, "订单")
        first_undo = undo_service.undo(self.conn, op)
        self.assertIn("订单", list_tables(self.conn))
        second_undo = undo_service.undo(self.conn, first_undo)
        self.assertNotIn("订单", list_tables(self.conn))
        undo_service.undo(self.conn, second_undo)
        self.assertIn("订单", list_tables(self.conn))
        self.assertEqual(self.count('SELECT COUNT(*) FROM "订单"'), 2)

    def test_undo_purge_table_is_irreversible(self):
        drop_table(self.conn, "订单")
        from local_sql_manager.services.schema import purge_table

        op = purge_table(self.conn, "订单")
        with self.assertRaises(Irreversible):
            undo_service.undo(self.conn, op)


class TestUndoRegistry(BaseUndoTestCase):
    def test_register_custom_kind(self):
        def handler(conn, op, source):
            return {"actions": [undo_service.trash_table(conn, "订单")]}

        self.swap_handler("import", handler)
        op = audit.record(
            self.conn, "import", "订单", "web", {"tables": ["订单"]}
        )
        undo_id = undo_service.undo(self.conn, op)
        record = self.op(undo_id)
        self.assertEqual(record["kind"], "undo")
        self.assertEqual(record["payload"]["op"], op)
        self.assertEqual(record["payload"]["actions"][0]["action"], "drop_table")
        self.assertNotIn("订单", list_tables(self.conn))

        undo_service.undo(self.conn, undo_id)
        self.assertIn("订单", list_tables(self.conn))

    def test_register_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            undo_service.register("", lambda conn, op, source: None)
        with self.assertRaises(ValueError):
            undo_service.register(None, lambda conn, op, source: None)
        with self.assertRaises(TypeError):
            undo_service.register("import", "not-callable")

    def test_multiple_actions_are_undone_in_reverse_order(self):
        def handler(conn, op, source):
            # 先 订单→发票，再 发票→单据；撤销必须倒着走，否则中间名不存在。
            return {
                "actions": [
                    undo_service._rename_action(conn, "订单", "发票"),
                    undo_service._rename_action(conn, "发票", "单据"),
                ]
            }

        self.swap_handler("merge", handler)
        op = audit.record(self.conn, "merge", "订单", "web", {})
        undo_id = undo_service.undo(self.conn, op)
        self.assertIn("单据", list_tables(self.conn))

        undo_service.undo(self.conn, undo_id)
        self.assertIn("订单", list_tables(self.conn))
        self.assertNotIn("单据", list_tables(self.conn))

    def test_registered_handler_can_be_replaced(self):
        calls = []

        def first(conn, op, source):
            calls.append("first")
            return {}

        def second(conn, op, source):
            calls.append("second")
            return {}

        self.swap_handler("merge", first)
        undo_service.register("merge", second)
        op = audit.record(self.conn, "merge", "订单", "web", {})
        undo_service.undo(self.conn, op)
        self.assertEqual(calls, ["second"])


if __name__ == "__main__":
    unittest.main()
