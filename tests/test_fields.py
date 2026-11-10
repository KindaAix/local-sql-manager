"""字段级操作（增/改名/删/改类型）与撤销的服务层测试。"""

import base64
import importlib
import json
import os
import sqlite3
import tempfile
import unittest

from local_sql_manager.services import fields, undo as undo_service, views
from local_sql_manager.services.audit import get_op
from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.models import StaleOperation, ViewConfig
from local_sql_manager.services.rows import insert_rows, update_cells
from local_sql_manager.services.schema import ColumnSpec, create_table, drop_table, get_fields
from local_sql_manager.services.util import utcnow_iso

OLD_TS = "2020-01-01T00:00:00Z"


def full_config():
    return ViewConfig(
        filters=[{"column": "金额", "op": "gt", "value": 10}],
        sorts=[{"column": "金额", "dir": "desc"}],
        hidden=["金额"],
        order=["金额", "备注", "状态"],
        widths={"金额": 220, "备注": 160},
        group={"column": "金额", "collapsed": True},
        aggregates=[{"column": "金额", "fn": "sum"}],
    )


class BaseFieldCase(unittest.TestCase):
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
                ColumnSpec("备注", "text"),
            ],
        )
        self.first = self._seed("完成", 100, "首单")
        self.second = self._seed("新建", 50, None)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _seed(self, status, amount, note):
        cur = self.conn.execute(
            'INSERT INTO "订单" ("created_at", "updated_at", "状态", "金额", "备注")'
            " VALUES (?, ?, ?, ?, ?)",
            (OLD_TS, OLD_TS, status, amount, note),
        )
        return cur.lastrowid

    def names(self, table="订单"):
        return [field.name for field in get_fields(self.conn, table)]

    def columns(self, table="订单"):
        return [row[1] for row in self.conn.execute(f'PRAGMA table_info("{table}")')]

    def indexes(self, table="订单"):
        return sorted(row[1] for row in self.conn.execute(f'PRAGMA index_list("{table}")'))

    def cell(self, row_id, column):
        return self.conn.execute(
            f'SELECT "{column}" FROM "订单" WHERE "id" = ?', (row_id,)
        ).fetchone()[0]

    def op(self, op_id):
        return get_op(self.conn, op_id)

    def count(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]

    def add_view(self, config=None, name="表格", table="订单"):
        return views.create_view(self.conn, table, name, config or full_config())

    def stored_config(self, view_id):
        raw = self.conn.execute(
            'SELECT "config_json" FROM "__lsm_view" WHERE "id" = ?', (view_id,)
        ).fetchone()[0]
        return json.loads(raw)


class TestAddField(BaseFieldCase):
    def test_existing_rows_are_empty(self):
        fields.add_field(self.conn, "订单", ColumnSpec("备注2", "text"))
        self.assertIsNone(self.cell(self.first, "备注2"))
        self.assertIsNone(self.cell(self.second, "备注2"))

    def test_appends_at_end_by_default(self):
        fields.add_field(self.conn, "订单", ColumnSpec("备注2", "text"))
        self.assertEqual(self.names(), ["状态", "金额", "备注", "备注2"])

    def test_insert_after_position_and_shift(self):
        fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"), after="金额")
        self.assertEqual(self.names(), ["状态", "金额", "折扣", "备注"])

    def test_insert_after_rejects_unknown_column(self):
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"), after="不存在")

    def test_insert_on_first_column(self):
        fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"), after="状态")
        self.assertEqual(self.names(), ["状态", "折扣", "金额", "备注"])

    def test_rejects_duplicate_name(self):
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "订单", ColumnSpec("金额", "text"))

    def test_rejects_duplicate_name_case_insensitive(self):
        create_table(self.conn, "abc", [ColumnSpec("Area", "text")])
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "abc", ColumnSpec("area", "text"))

    def test_rejects_reserved_names(self):
        for name in ("id", "ID", "created_at", "updated_at", "deleted_at", "rowid"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    fields.add_field(self.conn, "订单", ColumnSpec(name, "text"))

    def test_rejects_blank_name(self):
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "订单", ColumnSpec("   ", "text"))

    def test_rejects_non_string_name(self):
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "订单", ColumnSpec(None, "text"))

    def test_rejects_non_string_rename_target(self):
        with self.assertRaises(ValueError):
            fields.rename_field(self.conn, "订单", "金额", None)

    def test_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "订单", ColumnSpec("x", "currency"))

    def test_rejects_options_on_plain_type(self):
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "订单", ColumnSpec("x", "text", ("a",)))

    def test_rejects_non_text_options(self):
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "订单", ColumnSpec("x", "select", (1, 2)))

    def test_rejects_bare_string_options(self):
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "订单", ColumnSpec("标签", "select", "abc"))
        with self.assertRaises(ValueError):
            fields.set_field_type(self.conn, "订单", "状态", "select", "abc")

    def test_rejects_unmanaged_table(self):
        self.conn.execute('CREATE TABLE "外部" ("a" TEXT)')
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "外部", ColumnSpec("b", "text"))
        self.assertNotIn("b", self.columns("外部"))

    def test_rejects_table_without_active_status(self):
        self.conn.execute(
            'UPDATE "__lsm_table" SET "status" = NULL WHERE "table_name" = ?', ("订单",)
        )
        with self.assertRaises(ValueError):
            fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))

    def test_creates_index(self):
        fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        self.assertIn("__lsm_idx_2_订单_2_折扣", self.indexes())

    def test_uses_default_width(self):
        fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        width = self.conn.execute(
            'SELECT "width" FROM "__lsm_field" WHERE "table_name" = ? AND "column" = ?',
            ("订单", "折扣"),
        ).fetchone()[0]
        self.assertEqual(width, 140)

    def test_records_audit(self):
        op = fields.add_field(self.conn, "订单", ColumnSpec("标签", "select", ("a", "b")))
        record = self.op(op)
        self.assertEqual(record["kind"], "create_field")
        self.assertEqual(record["table"], "订单")
        self.assertEqual(record["source"], "web")
        self.assertEqual(record["irreversible"], 0)
        self.assertEqual(
            record["payload"],
            {"column": "标签", "type": "select", "options": ["a", "b"], "position": 3},
        )

    def test_select_options_round_trip(self):
        fields.add_field(self.conn, "订单", ColumnSpec("标签", "select", ("a", "b")))
        field = [f for f in get_fields(self.conn, "订单") if f.name == "标签"][0]
        self.assertEqual(field.options, ("a", "b"))

    def test_does_not_commit(self):
        fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertNotIn("折扣", self.columns())


class TestRenameField(BaseFieldCase):
    def test_renames_column_and_keeps_data(self):
        fields.rename_field(self.conn, "订单", "金额", "金额元")
        self.assertIn("金额元", self.columns())
        self.assertNotIn("金额", self.columns())
        self.assertEqual(self.cell(self.first, "金额元"), 100)

    def test_updates_metadata_in_place(self):
        fields.rename_field(self.conn, "订单", "金额", "金额元")
        self.assertEqual(self.names(), ["状态", "金额元", "备注"])
        field = [f for f in get_fields(self.conn, "订单") if f.name == "金额元"][0]
        self.assertEqual((field.type, field.position, field.options), ("number", 1, ()))

    def test_renames_index(self):
        fields.rename_field(self.conn, "订单", "金额", "金额元")
        names = self.indexes()
        self.assertIn("__lsm_idx_2_订单_3_金额元", names)
        self.assertNotIn("__lsm_idx_2_订单_2_金额", names)

    def test_view_config_follows_everywhere(self):
        view_id = self.add_view()
        fields.rename_field(self.conn, "订单", "金额", "金额元")
        config = views.load_config(self.conn, view_id)
        self.assertEqual(config.filters, [{"column": "金额元", "op": "gt", "value": 10}])
        self.assertEqual(config.sorts, [{"column": "金额元", "dir": "desc"}])
        self.assertEqual(config.hidden, ["金额元"])
        self.assertEqual(config.order, ["金额元", "备注", "状态"])
        self.assertEqual(config.widths, {"金额元": 220, "备注": 160})
        self.assertEqual(config.group, {"column": "金额元", "collapsed": True})
        self.assertEqual(config.aggregates, [{"column": "金额元", "fn": "sum"}])

    def test_updates_all_views_of_the_table(self):
        first = self.add_view(name="表格")
        second = self.add_view(name="看板")
        fields.rename_field(self.conn, "订单", "金额", "金额元")
        for view_id in (first, second):
            self.assertEqual(views.load_config(self.conn, view_id).sorts[0]["column"], "金额元")

    def test_leaves_other_tables_views_alone(self):
        create_table(self.conn, "客户", [ColumnSpec("金额", "text")])
        other = views.create_view(
            self.conn, "客户", "表", ViewConfig(sorts=[{"column": "金额", "dir": "asc"}])
        )
        fields.rename_field(self.conn, "订单", "金额", "金额元")
        self.assertEqual(
            views.load_config(self.conn, other).sorts[0]["column"], "金额"
        )

    def test_keeps_unknown_config_keys(self):
        view_id = self.add_view(ViewConfig(sorts=[{"column": "金额", "dir": "asc"}]))
        self.conn.execute(
            'UPDATE "__lsm_view" SET "config_json" = ? WHERE "id" = ?',
            (json.dumps({"sorts": [{"column": "金额", "dir": "asc"}], "row_height": 40}), view_id),
        )
        fields.rename_field(self.conn, "订单", "金额", "金额元")
        self.assertEqual(self.stored_config(view_id)["row_height"], 40)

    def test_rejects_existing_name(self):
        with self.assertRaises(ValueError):
            fields.rename_field(self.conn, "订单", "金额", "备注")

    def test_rejects_existing_name_case_insensitive(self):
        create_table(self.conn, "abc", [ColumnSpec("Area", "text")])
        with self.assertRaises(ValueError) as ctx:
            fields.rename_field(self.conn, "abc", "Area", "area")
        self.assertIn("不区分大小写", str(ctx.exception))

    def test_rejects_reserved_name(self):
        with self.assertRaises(ValueError):
            fields.rename_field(self.conn, "订单", "金额", "updated_at")

    def test_rejects_same_name(self):
        with self.assertRaises(ValueError):
            fields.rename_field(self.conn, "订单", "金额", "金额")

    def test_rejects_unknown_column(self):
        with self.assertRaises(ValueError):
            fields.rename_field(self.conn, "订单", "不存在", "新列")

    def test_rejects_unmanaged_table(self):
        self.conn.execute('CREATE TABLE "外部" ("a" TEXT)')
        with self.assertRaises(ValueError):
            fields.rename_field(self.conn, "外部", "a", "b")

    def test_records_audit(self):
        op = fields.rename_field(self.conn, "订单", "金额", "金额元")
        record = self.op(op)
        self.assertEqual(record["kind"], "rename_field")
        self.assertEqual(record["payload"], {"old": "金额", "new": "金额元"})

    def test_does_not_commit(self):
        fields.rename_field(self.conn, "订单", "金额", "金额元")
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIn("金额", self.columns())


class TestDropField(BaseFieldCase):
    def test_removes_column_and_metadata(self):
        fields.drop_field(self.conn, "订单", "金额")
        self.assertNotIn("金额", self.columns())
        self.assertEqual(self.names(), ["状态", "备注"])
        self.assertEqual(
            self.count('SELECT COUNT(*) FROM "__lsm_field" WHERE "column" = ?', ("金额",)), 0
        )

    def test_drops_index(self):
        fields.drop_field(self.conn, "订单", "金额")
        self.assertNotIn("__lsm_idx_2_订单_2_金额", self.indexes())

    def test_backs_up_values(self):
        op = fields.drop_field(self.conn, "订单", "金额")
        rows = self.conn.execute(
            'SELECT "op_id", "table_name", "column", "type", "row_id", "value"'
            ' FROM "__lsm_trash_col" ORDER BY "row_id"'
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (op, "订单", "金额", "number", self.first, "100.0"),
                (op, "订单", "金额", "number", self.second, "50.0"),
            ],
        )

    def test_skips_null_values(self):
        op = fields.drop_field(self.conn, "订单", "备注")
        rows = self.conn.execute(
            'SELECT "row_id" FROM "__lsm_trash_col" WHERE "op_id" = ?', (op,)
        ).fetchall()
        self.assertEqual([row[0] for row in rows], [self.first])

    def test_backs_up_blob_as_base64(self):
        payload = b"\x00\x01\xff"
        self.conn.execute(
            'UPDATE "订单" SET "备注" = ? WHERE "id" = ?', (payload, self.second)
        )
        op = fields.drop_field(self.conn, "订单", "备注")
        values = [
            json.loads(row[0])
            for row in self.conn.execute(
                'SELECT "value" FROM "__lsm_trash_col" WHERE "op_id" = ? ORDER BY "row_id"',
                (op,),
            )
        ]
        self.assertEqual(values[1], {"b64": base64.b64encode(payload).decode("ascii")})

    def test_view_config_loses_the_column(self):
        view_id = self.add_view()
        fields.drop_field(self.conn, "订单", "金额")
        config = views.load_config(self.conn, view_id)
        self.assertEqual(config.filters, [])
        self.assertEqual(config.sorts, [])
        self.assertEqual(config.aggregates, [])
        self.assertEqual(config.hidden, [])
        self.assertEqual(config.order, ["备注", "状态"])
        self.assertEqual(config.widths, {"备注": 160})
        self.assertIsNone(config.group)

    def test_records_audit(self):
        op = fields.drop_field(self.conn, "订单", "金额")
        record = self.op(op)
        self.assertEqual(record["kind"], "drop_field")
        self.assertEqual(record["table"], "订单")
        payload = record["payload"]
        self.assertEqual(payload["column"], "金额")
        self.assertEqual(payload["type"], "number")
        self.assertEqual(payload["options"], [])
        self.assertEqual(payload["position"], 1)
        self.assertEqual(payload["backup_rows"], 2)

    def test_drops_extra_indexes_on_the_column(self):
        self.conn.execute('CREATE INDEX "我的索引" ON "订单" ("金额")')
        fields.drop_field(self.conn, "订单", "金额")
        self.assertNotIn("我的索引", self.indexes())
        self.assertNotIn("金额", self.columns())

    def test_expression_index_fails_with_chinese_message(self):
        quote = "'"
        self.conn.execute(
            f'CREATE INDEX "表达式索引" ON "订单" ("金额" || {quote}{quote})'
        )
        with self.assertRaises(ValueError) as ctx:
            fields.drop_field(self.conn, "订单", "金额")
        self.assertIn("无法删除列", str(ctx.exception))
        self.assertIn("金额", self.columns())

    def test_earlier_row_undo_still_works(self):
        # 回归：payload 里曾用 "rows": <int>，undo 会把它当行差分遍历后崩溃。
        insert_op = insert_rows(self.conn, "订单", [{"备注": "后加"}])
        fields.drop_field(self.conn, "订单", "金额")
        undo_service.undo(self.conn, insert_op)
        self.assertEqual(self.count('SELECT COUNT(*) FROM "订单"'), 2)

    def test_rejects_unknown_column(self):
        with self.assertRaises(ValueError):
            fields.drop_field(self.conn, "订单", "不存在")

    def test_rejects_unmanaged_table(self):
        self.conn.execute('CREATE TABLE "外部" ("a" TEXT)')
        with self.assertRaises(ValueError):
            fields.drop_field(self.conn, "外部", "a")

    def test_last_column_can_be_dropped(self):
        fields.drop_field(self.conn, "订单", "状态")
        fields.drop_field(self.conn, "订单", "金额")
        fields.drop_field(self.conn, "订单", "备注")
        self.assertEqual(self.names(), [])
        self.assertEqual(self.count('SELECT COUNT(*) FROM "订单"'), 2)

    def test_does_not_commit(self):
        fields.drop_field(self.conn, "订单", "金额")
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIn("金额", self.columns())
        self.assertEqual(self.count('SELECT COUNT(*) FROM "__lsm_trash_col"'), 0)


class TestSetFieldType(BaseFieldCase):
    def test_changes_metadata_only(self):
        op = fields.set_field_type(self.conn, "订单", "金额", "text")
        field = [f for f in get_fields(self.conn, "订单") if f.name == "金额"][0]
        self.assertEqual(field.type, "text")
        self.assertEqual(self.cell(self.first, "金额"), 100.0)
        raw = self.conn.execute(
            'SELECT "type" FROM "__lsm_field" WHERE "table_name" = ? AND "column" = ?',
            ("订单", "金额"),
        ).fetchone()[0]
        self.assertEqual(raw, "text")
        self.assertEqual(self.op(op)["kind"], "set_field_type")

    def test_keeps_position_and_width(self):
        fields.set_field_type(self.conn, "订单", "金额", "text")
        field = [f for f in get_fields(self.conn, "订单") if f.name == "金额"][0]
        self.assertEqual((field.position, field.width), (1, 140))

    def test_stores_new_options(self):
        fields.set_field_type(self.conn, "订单", "状态", "multiselect", ("新建", "完成", "搁置"))
        field = [f for f in get_fields(self.conn, "订单") if f.name == "状态"][0]
        self.assertEqual(field.options, ("新建", "完成", "搁置"))

    def test_clears_options(self):
        fields.set_field_type(self.conn, "订单", "状态", "text")
        field = [f for f in get_fields(self.conn, "订单") if f.name == "状态"][0]
        self.assertEqual(field.options, ())

    def test_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            fields.set_field_type(self.conn, "订单", "金额", "currency")

    def test_rejects_options_on_plain_type(self):
        with self.assertRaises(ValueError):
            fields.set_field_type(self.conn, "订单", "金额", "text", ("a",))

    def test_rejects_unknown_column(self):
        with self.assertRaises(ValueError):
            fields.set_field_type(self.conn, "订单", "不存在", "text")

    def test_rejects_unmanaged_table(self):
        self.conn.execute('CREATE TABLE "外部" ("a" TEXT)')
        with self.assertRaises(ValueError):
            fields.set_field_type(self.conn, "外部", "a", "text")

    def test_records_audit(self):
        op = fields.set_field_type(self.conn, "订单", "状态", "multiselect", ("新建", "完成", "搁置"))
        record = self.op(op)
        self.assertEqual(record["kind"], "set_field_type")
        self.assertEqual(
            record["payload"],
            {
                "column": "状态",
                "from": "select",
                "to": "multiselect",
                "options": ["新建", "完成", "搁置"],
                "old_options": ["新建", "完成"],
            },
        )

    def test_does_not_commit(self):
        fields.set_field_type(self.conn, "订单", "金额", "text")
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        field = [f for f in get_fields(self.conn, "订单") if f.name == "金额"][0]
        self.assertEqual(field.type, "number")


class TestUndoCreateField(BaseFieldCase):
    def test_removes_the_new_column(self):
        op = fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        undo_service.undo(self.conn, op)
        self.assertNotIn("折扣", self.columns())
        self.assertEqual(self.names(), ["状态", "金额", "备注"])

    def test_removes_index_and_view_refs(self):
        op = fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        view_id = self.add_view(ViewConfig(order=["折扣", "金额"]))
        undo_service.undo(self.conn, op)
        self.assertNotIn("__lsm_idx_2_订单_2_折扣", self.indexes())
        self.assertEqual(views.load_config(self.conn, view_id).order, ["金额"])

    def test_records_undo_op(self):
        op = fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        undo_id = undo_service.undo(self.conn, op)
        record = self.op(undo_id)
        self.assertEqual(record["kind"], "undo")
        self.assertEqual(record["payload"]["op"], op)
        self.assertEqual(self.op(op)["undone_by"], undo_id)

    def test_stale_when_cell_was_written(self):
        op = fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        update_cells(
            self.conn,
            "订单",
            {self.first: {"折扣": 1}},
            {self.first: self.cell(self.first, "updated_at")},
        )
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertIn("折扣", self.columns())

    def test_stale_when_column_holds_data_without_audit(self):
        op = fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        self.conn.execute('UPDATE "订单" SET "折扣" = 5 WHERE "id" = ?', (self.first,))
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)
        self.assertIn("折扣", self.columns())

    def test_stale_when_renamed_after(self):
        op = fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        fields.rename_field(self.conn, "订单", "折扣", "折扣率")
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_stale_when_type_changed_after(self):
        op = fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        fields.set_field_type(self.conn, "订单", "折扣", "text")
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_allowed_after_unrelated_write(self):
        op = fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        update_cells(
            self.conn,
            "订单",
            {self.first: {"备注": "改过"}},
            {self.first: self.cell(self.first, "updated_at")},
        )
        undo_service.undo(self.conn, op)
        self.assertNotIn("折扣", self.columns())
        self.assertEqual(self.cell(self.first, "备注"), "改过")


class TestUndoRenameField(BaseFieldCase):
    def test_renames_back(self):
        op = fields.rename_field(self.conn, "订单", "金额", "金额元")
        undo_service.undo(self.conn, op)
        self.assertIn("金额", self.columns())
        self.assertNotIn("金额元", self.columns())
        self.assertEqual(self.cell(self.first, "金额"), 100)
        self.assertEqual(self.names(), ["状态", "金额", "备注"])

    def test_restores_index_and_views(self):
        view_id = self.add_view()
        op = fields.rename_field(self.conn, "订单", "金额", "金额元")
        undo_service.undo(self.conn, op)
        self.assertIn("__lsm_idx_2_订单_2_金额", self.indexes())
        self.assertNotIn("__lsm_idx_2_订单_3_金额元", self.indexes())
        self.assertEqual(views.load_config(self.conn, view_id).sorts[0]["column"], "金额")

    def test_stale_when_old_name_reused(self):
        op = fields.rename_field(self.conn, "订单", "金额", "金额元")
        fields.add_field(self.conn, "订单", ColumnSpec("金额", "text"))
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_stale_when_renamed_away(self):
        op = fields.rename_field(self.conn, "订单", "金额", "金额元")
        fields.rename_field(self.conn, "订单", "金额元", "总金额")
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)


class TestUndoDropField(BaseFieldCase):
    def test_rebuilds_column_and_values(self):
        op = fields.drop_field(self.conn, "订单", "备注")
        undo_service.undo(self.conn, op)
        self.assertIn("备注", self.columns())
        self.assertEqual(self.cell(self.first, "备注"), "首单")
        self.assertIsNone(self.cell(self.second, "备注"))
        self.assertEqual(self.names(), ["状态", "金额", "备注"])

    def test_undo_rebuilds_system_index_after_extra_index_drop(self):
        self.conn.execute('CREATE INDEX "我的索引" ON "订单" ("金额")')
        op = fields.drop_field(self.conn, "订单", "金额")
        undo_service.undo(self.conn, op)
        self.assertIn("__lsm_idx_2_订单_2_金额", self.indexes())

    def test_restores_type_and_index(self):
        op = fields.drop_field(self.conn, "订单", "金额")
        undo_service.undo(self.conn, op)
        field = [f for f in get_fields(self.conn, "订单") if f.name == "金额"][0]
        self.assertEqual(field.type, "number")
        self.assertIn("__lsm_idx_2_订单_2_金额", self.indexes())

    def test_restores_view_refs(self):
        view_id = self.add_view()
        op = fields.drop_field(self.conn, "订单", "金额")
        undo_service.undo(self.conn, op)
        config = views.load_config(self.conn, view_id)
        self.assertEqual(config.filters, [{"column": "金额", "op": "gt", "value": 10}])
        self.assertEqual(config.sorts, [{"column": "金额", "dir": "desc"}])
        self.assertEqual(config.hidden, ["金额"])
        self.assertEqual(config.order, ["金额", "备注", "状态"])
        self.assertEqual(config.widths, {"金额": 220, "备注": 160})
        self.assertEqual(config.group, {"column": "金额", "collapsed": True})
        self.assertEqual(config.aggregates, [{"column": "金额", "fn": "sum"}])

    def test_clears_trash_rows(self):
        op = fields.drop_field(self.conn, "订单", "金额")
        undo_service.undo(self.conn, op)
        self.assertEqual(
            self.count('SELECT COUNT(*) FROM "__lsm_trash_col" WHERE "op_id" = ?', (op,)), 0
        )

    def test_restores_sort_and_aggregate_order(self):
        view_id = self.add_view(
            ViewConfig(
                sorts=[
                    {"column": "备注", "dir": "asc"},
                    {"column": "金额", "dir": "desc"},
                    {"column": "状态", "dir": "asc"},
                ],
                aggregates=[
                    {"column": "备注", "fn": "count"},
                    {"column": "金额", "fn": "sum"},
                    {"column": "状态", "fn": "count"},
                ],
            )
        )
        op = fields.drop_field(self.conn, "订单", "金额")
        undo_service.undo(self.conn, op)
        config = views.load_config(self.conn, view_id)
        self.assertEqual(
            [item["column"] for item in config.sorts], ["备注", "金额", "状态"]
        )
        self.assertEqual(
            [item["column"] for item in config.aggregates], ["备注", "金额", "状态"]
        )

    def test_undo_keeps_zero_width(self):
        self.conn.execute(
            'UPDATE "__lsm_field" SET "width" = 0'
            ' WHERE "table_name" = ? AND "column" = ?',
            ("订单", "金额"),
        )
        op = fields.drop_field(self.conn, "订单", "金额")
        undo_service.undo(self.conn, op)
        width = self.conn.execute(
            'SELECT "width" FROM "__lsm_field"'
            ' WHERE "table_name" = ? AND "column" = ?',
            ("订单", "金额"),
        ).fetchone()[0]
        self.assertEqual(width, 0)

    def test_undo_twice_is_rejected(self):
        op = fields.drop_field(self.conn, "订单", "金额")
        undo_service.undo(self.conn, op)
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_stale_when_name_reused(self):
        op = fields.drop_field(self.conn, "订单", "金额")
        fields.add_field(self.conn, "订单", ColumnSpec("金额", "text"))
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_stale_when_table_dropped(self):
        op = fields.drop_field(self.conn, "订单", "金额")
        drop_table(self.conn, "订单")
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_undo_of_field_undo_is_not_supported(self):
        # 字段撤销记录不参与「撤销的撤销」（层 3 的 undo 只认表级动作）。
        op = fields.drop_field(self.conn, "订单", "备注")
        undo_id = undo_service.undo(self.conn, op)
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, undo_id)
        self.assertEqual(self.cell(self.first, "备注"), "首单")

    def test_restores_blob(self):
        payload = b"\x00\x07"
        self.conn.execute('UPDATE "订单" SET "备注" = ? WHERE "id" = ?', (payload, self.second))
        op = fields.drop_field(self.conn, "订单", "备注")
        undo_service.undo(self.conn, op)
        self.assertEqual(self.cell(self.second, "备注"), payload)


class TestUndoSetFieldType(BaseFieldCase):
    def test_restores_type_and_options(self):
        op = fields.set_field_type(self.conn, "订单", "状态", "multiselect", ("新建", "完成", "搁置"))
        undo_service.undo(self.conn, op)
        field = [f for f in get_fields(self.conn, "订单") if f.name == "状态"][0]
        self.assertEqual(field.type, "select")
        self.assertEqual(field.options, ("新建", "完成"))

    def test_stale_after_a_second_type_change(self):
        op = fields.set_field_type(self.conn, "订单", "金额", "text")
        fields.set_field_type(self.conn, "订单", "金额", "date")
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)

    def test_stale_when_column_dropped(self):
        op = fields.set_field_type(self.conn, "订单", "金额", "text")
        fields.drop_field(self.conn, "订单", "金额")
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)


class TestRegistration(BaseFieldCase):
    def test_handlers_registered(self):
        for kind in ("create_field", "rename_field", "drop_field", "set_field_type"):
            self.assertIn(kind, undo_service._HANDLERS)

    def test_registration_is_idempotent(self):
        importlib.reload(fields)
        for kind in ("create_field", "rename_field", "drop_field", "set_field_type"):
            self.assertIn(kind, undo_service._HANDLERS)
        op = fields.add_field(self.conn, "订单", ColumnSpec("折扣", "number"))
        undo_service.undo(self.conn, op)
        self.assertNotIn("折扣", self.columns())

    def test_field_ops_block_undo_of_create_table(self):
        from local_sql_manager.services import audit

        create_table(self.conn, "临时", [ColumnSpec("a", "text")])
        op = audit.record(self.conn, "create_table", "临时", "web", {"name": "临时"})
        fields.add_field(self.conn, "临时", ColumnSpec("折扣", "number"))
        with self.assertRaises(StaleOperation):
            undo_service.undo(self.conn, op)


class TestNoCommitWithFileDb(unittest.TestCase):
    """文件库 + 第二条连接：未提交的 DDL/DML 对别人不可见。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "lib.sqlite")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        now = utcnow_iso()
        self.conn.execute(
            'INSERT INTO "订单" ("created_at", "updated_at", "金额") VALUES (?, ?, ?)',
            (now, now, 100),
        )
        self.conn.commit()
        self.other = sqlite3.connect(self.path)
        self.other.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()
        self.other.close()
        self.tmp.cleanup()

    def other_columns(self):
        return [row[1] for row in self.other.execute('PRAGMA table_info("订单")')]

    def assert_uncommitted(self):
        self.assertTrue(self.conn.in_transaction)
        self.assertIn("金额", self.other_columns())

    def test_add_field(self):
        fields.add_field(self.conn, "订单", ColumnSpec("备注", "text"))
        self.assert_uncommitted()
        self.assertNotIn("备注", self.other_columns())
        self.conn.rollback()

    def test_rename_field(self):
        fields.rename_field(self.conn, "订单", "金额", "金额元")
        self.assert_uncommitted()
        self.conn.rollback()

    def test_drop_field(self):
        fields.drop_field(self.conn, "订单", "金额")
        self.assert_uncommitted()
        self.assertEqual(self.other.execute('SELECT COUNT(*) FROM "__lsm_trash_col"').fetchone()[0], 0)
        self.conn.rollback()

    def test_set_field_type(self):
        fields.set_field_type(self.conn, "订单", "金额", "text")
        self.assert_uncommitted()
        row = self.other.execute('SELECT "type" FROM "__lsm_field"').fetchone()
        self.assertEqual(row[0], "number")
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()