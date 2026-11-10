"""视图配置的存取。"""

import json
import sqlite3
import unittest

from local_sql_manager.services import audit
from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.models import ViewConfig
from local_sql_manager.services.schema import ColumnSpec, create_table
from local_sql_manager.services.views import (
    create_view,
    default_view,
    delete_view,
    list_views,
    load_config,
    set_default,
    update_view,
)


class TestViews(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        create_table(
            self.conn,
            "订单",
            [ColumnSpec("金额", "number"), ColumnSpec("状态", "select", ("新建", "完成"))],
        )

    def tearDown(self):
        self.conn.close()

    def _rows(self, table="__lsm_view"):
        return self.conn.execute(f'SELECT * FROM "{table}"').fetchall()

    def _make_default(self):
        """落库一个默认视图，返回其 id。"""
        return create_view(self.conn, "订单", "全部记录", ViewConfig())

    def _ops(self):
        return [
            dict(row)
            for row in self.conn.execute(
                'SELECT * FROM "__lsm_op" ORDER BY "id"'
            ).fetchall()
        ]

    def test_default_view_helper(self):
        view = default_view("订单")
        self.assertEqual(view["name"], "全部记录")
        self.assertEqual(view["config"], ViewConfig())

    def test_first_list_views_returns_volatile_default(self):
        views = list_views(self.conn, "订单")
        self.assertEqual(len(views), 1)
        self.assertIsNone(views[0]["id"])
        self.assertEqual(views[0]["table"], "订单")
        self.assertEqual(views[0]["name"], "全部记录")
        self.assertTrue(views[0]["is_default"])
        self.assertEqual(views[0]["config"], ViewConfig())
        self.assertEqual(self._rows(), [])

    def test_list_views_does_not_persist_default(self):
        list_views(self.conn, "订单")
        list_views(self.conn, "订单")
        self.assertEqual(self._rows(), [])

    def test_list_views_unknown_table_is_empty(self):
        self.assertEqual(list_views(self.conn, "不存在的表"), [])

    def test_create_view_appends_and_returns_id(self):
        view_id = create_view(
            self.conn, "订单", "本月", ViewConfig(hidden=["状态"])
        )
        self.assertIsInstance(view_id, int)
        views = list_views(self.conn, "订单")
        self.assertEqual([v["name"] for v in views], ["本月"])
        self.assertEqual([v["position"] for v in views], [0])
        self.assertTrue(views[0]["is_default"])

    def test_create_view_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            create_view(self.conn, "订单", "  ", ViewConfig())

    def test_create_view_rejects_missing_table(self):
        with self.assertRaises(ValueError):
            create_view(self.conn, "不存在的表", "视图", ViewConfig())

    def test_config_roundtrip(self):
        config = ViewConfig(
            filters=[{"column": "状态", "op": "eq", "value": "完成"}],
            sorts=[{"column": "金额", "dir": "desc"}],
            hidden=["状态"],
            order=["金额", "状态"],
            widths={"金额": 220},
            group={"column": "状态", "collapsed": False},
            aggregates=[{"column": "金额", "fn": "sum"}],
        )
        view_id = create_view(self.conn, "订单", "本月", config)
        self.assertEqual(load_config(self.conn, view_id), config)

    def test_config_stored_as_json_text(self):
        view_id = create_view(self.conn, "订单", "本月", ViewConfig(hidden=["状态"]))
        raw = self.conn.execute(
            'SELECT "config_json" FROM "__lsm_view" WHERE "id" = ?', (view_id,)
        ).fetchone()[0]
        self.assertEqual(json.loads(raw)["hidden"], ["状态"])

    def test_load_config_missing_view_returns_empty(self):
        self.assertEqual(load_config(self.conn, 99999), ViewConfig())

    def test_load_config_none_returns_empty(self):
        self.assertEqual(load_config(self.conn, None), ViewConfig())

    def test_update_prunes_unknown_columns(self):
        view_id = self._make_default()
        update_view(self.conn, view_id, ViewConfig(hidden=["不存在", "金额"]))
        self.assertEqual(load_config(self.conn, view_id).hidden, ["金额"])

    def test_update_prunes_all_column_references(self):
        view_id = self._make_default()
        update_view(
            self.conn,
            view_id,
            ViewConfig(
                filters=[
                    {"column": "不存在", "op": "eq", "value": 1},
                    {"column": "状态", "op": "eq", "value": "完成"},
                ],
                sorts=[
                    {"column": "不存在", "dir": "asc"},
                    {"column": "金额", "dir": "desc"},
                ],
                hidden=["不存在", "状态"],
                order=["不存在", "金额", "状态"],
                widths={"不存在": 100, "金额": 220},
                group={"column": "不存在"},
                aggregates=[
                    {"column": "不存在", "fn": "sum"},
                    {"column": "金额", "fn": "sum"},
                ],
            ),
        )
        config = load_config(self.conn, view_id)
        self.assertEqual(
            config.filters, [{"column": "状态", "op": "eq", "value": "完成"}]
        )
        self.assertEqual(config.sorts, [{"column": "金额", "dir": "desc"}])
        self.assertEqual(config.hidden, ["状态"])
        self.assertEqual(config.order, ["金额", "状态"])
        self.assertEqual(config.widths, {"金额": 220})
        self.assertIsNone(config.group)
        self.assertEqual(config.aggregates, [{"column": "金额", "fn": "sum"}])

    def test_update_keeps_view_name(self):
        view_id = self._make_default()
        update_view(self.conn, view_id, ViewConfig(hidden=["状态"]))
        self.assertEqual(list_views(self.conn, "订单")[0]["name"], "全部记录")

    def test_update_missing_view_raises(self):
        with self.assertRaises(ValueError):
            update_view(self.conn, 99999, ViewConfig())

    def test_update_none_view_raises(self):
        with self.assertRaises(ValueError):
            update_view(self.conn, None, ViewConfig())

    def test_rename_view(self):
        view_id = self._make_default()
        update_view(self.conn, view_id, ViewConfig(), name="全部")
        self.assertEqual(list_views(self.conn, "订单")[0]["name"], "全部")

    def test_rename_rejects_empty_name(self):
        view_id = self._make_default()
        with self.assertRaises(ValueError):
            update_view(self.conn, view_id, ViewConfig(), name="")

    def test_default_view_cannot_be_deleted(self):
        view_id = self._make_default()
        with self.assertRaises(ValueError):
            delete_view(self.conn, view_id)
        self.assertEqual(len(list_views(self.conn, "订单")), 1)

    def test_delete_none_view_raises(self):
        with self.assertRaises(ValueError):
            delete_view(self.conn, None)

    def test_delete_custom_view(self):
        default_id = self._make_default()
        view_id = create_view(self.conn, "订单", "本月", ViewConfig())
        delete_view(self.conn, view_id)
        self.assertEqual(
            [v["id"] for v in list_views(self.conn, "订单")], [default_id]
        )

    def test_delete_missing_view_raises(self):
        with self.assertRaises(ValueError):
            delete_view(self.conn, 99999)

    def test_set_default_switches_flag(self):
        view_id = create_view(self.conn, "订单", "本月", ViewConfig())
        set_default(self.conn, "订单", view_id)
        views = {v["name"]: v for v in list_views(self.conn, "订单")}
        self.assertTrue(views["本月"]["is_default"])

    def test_set_default_none_view_raises(self):
        with self.assertRaises(ValueError):
            set_default(self.conn, "订单", None)

    def test_set_default_rejects_foreign_view(self):
        create_table(self.conn, "客户", [ColumnSpec("名称", "text")])
        other = create_view(self.conn, "客户", "全部记录", ViewConfig())
        with self.assertRaises(ValueError):
            set_default(self.conn, "订单", other)

    def test_create_view_is_audited(self):
        view_id = create_view(self.conn, "订单", "本月", ViewConfig())
        ops = self._ops()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["kind"], "view")
        self.assertEqual(ops[0]["table_name"], "订单")
        self.assertEqual(
            json.loads(ops[0]["payload_json"]),
            {"action": "create", "name": "本月", "view_id": view_id},
        )

    def test_update_config_only_is_not_audited(self):
        view_id = self._make_default()
        audit_rows = len(self._ops())
        update_view(self.conn, view_id, ViewConfig(hidden=["状态"]))
        self.assertEqual(len(self._ops()), audit_rows)

    def test_rename_is_audited(self):
        view_id = self._make_default()
        before = len(self._ops())
        update_view(self.conn, view_id, ViewConfig(), name="全部")
        ops = self._ops()[before:]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["kind"], "view")
        self.assertEqual(
            json.loads(ops[0]["payload_json"]),
            {
                "action": "rename",
                "name": "全部",
                "old_name": "全部记录",
                "view_id": view_id,
            },
        )

    def test_same_name_rename_is_not_audited(self):
        view_id = self._make_default()
        before = len(self._ops())
        update_view(self.conn, view_id, ViewConfig(), name="全部记录")
        self.assertEqual(len(self._ops()), before)

    def test_delete_view_is_audited(self):
        self._make_default()
        view_id = create_view(self.conn, "订单", "本月", ViewConfig())
        before = len(self._ops())
        delete_view(self.conn, view_id)
        ops = self._ops()[before:]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["kind"], "view")
        self.assertEqual(
            json.loads(ops[0]["payload_json"]),
            {"action": "delete", "name": "本月", "view_id": view_id},
        )

    def test_set_default_is_not_audited(self):
        view_id = create_view(self.conn, "订单", "本月", ViewConfig())
        before = len(self._ops())
        set_default(self.conn, "订单", view_id)
        self.assertEqual(len(self._ops()), before)

    def test_writes_are_not_committed(self):
        create_view(self.conn, "订单", "本月", ViewConfig())
        self.conn.rollback()
        self.assertEqual(self._rows(), [])
        self.assertEqual(self._ops(), [])
