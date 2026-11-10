"""视图配置的存取。"""

import json
import sqlite3
import unittest

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
        self.default_id = list_views(self.conn, "订单")[0]["id"]

    def tearDown(self):
        self.conn.close()

    def _rows(self, table="__lsm_view"):
        return self.conn.execute(f'SELECT * FROM "{table}"').fetchall()

    def test_default_view_helper(self):
        view = default_view("订单")
        self.assertEqual(view["name"], "全部记录")
        self.assertEqual(view["config"], ViewConfig())

    def test_creating_table_makes_default_view(self):
        views = list_views(self.conn, "订单")
        self.assertEqual(len(views), 1)
        self.assertTrue(views[0]["is_default"])
        self.assertEqual(views[0]["name"], "全部记录")
        self.assertEqual(views[0]["table"], "订单")
        self.assertEqual(views[0]["config"], ViewConfig())

    def test_default_view_created_only_once(self):
        list_views(self.conn, "订单")
        list_views(self.conn, "订单")
        self.assertEqual(len(self._rows()), 1)

    def test_list_views_unknown_table_is_empty(self):
        self.assertEqual(list_views(self.conn, "不存在的表"), [])
        self.assertEqual(len(self._rows()), 1)

    def test_create_view_appends_and_returns_id(self):
        view_id = create_view(
            self.conn, "订单", "本月", ViewConfig(hidden=["状态"])
        )
        self.assertIsInstance(view_id, int)
        views = list_views(self.conn, "订单")
        self.assertEqual([v["name"] for v in views], ["全部记录", "本月"])
        self.assertEqual([v["position"] for v in views], [0, 1])
        self.assertFalse(views[1]["is_default"])

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

    def test_update_prunes_unknown_columns(self):
        update_view(
            self.conn, self.default_id, ViewConfig(hidden=["不存在", "金额"])
        )
        self.assertEqual(load_config(self.conn, self.default_id).hidden, ["金额"])

    def test_update_prunes_all_column_references(self):
        update_view(
            self.conn,
            self.default_id,
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
        config = load_config(self.conn, self.default_id)
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
        update_view(self.conn, self.default_id, ViewConfig(hidden=["状态"]))
        views = list_views(self.conn, "订单")
        self.assertEqual(views[0]["name"], "全部记录")

    def test_update_missing_view_raises(self):
        with self.assertRaises(ValueError):
            update_view(self.conn, 99999, ViewConfig())

    def test_rename_view(self):
        update_view(self.conn, self.default_id, ViewConfig(), name="全部")
        self.assertEqual(list_views(self.conn, "订单")[0]["name"], "全部")

    def test_rename_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            update_view(self.conn, self.default_id, ViewConfig(), name="")

    def test_default_view_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            delete_view(self.conn, self.default_id)
        self.assertEqual(len(list_views(self.conn, "订单")), 1)

    def test_delete_custom_view(self):
        view_id = create_view(self.conn, "订单", "本月", ViewConfig())
        delete_view(self.conn, view_id)
        self.assertEqual([v["name"] for v in list_views(self.conn, "订单")], ["全部记录"])

    def test_delete_missing_view_raises(self):
        with self.assertRaises(ValueError):
            delete_view(self.conn, 99999)

    def test_set_default_switches_flag(self):
        view_id = create_view(self.conn, "订单", "本月", ViewConfig())
        set_default(self.conn, "订单", view_id)
        views = {v["name"]: v for v in list_views(self.conn, "订单")}
        self.assertTrue(views["本月"]["is_default"])
        self.assertFalse(views["全部记录"]["is_default"])

    def test_set_default_rejects_foreign_view(self):
        create_table(self.conn, "客户", [ColumnSpec("名称", "text")])
        other = list_views(self.conn, "客户")[0]["id"]
        with self.assertRaises(ValueError):
            set_default(self.conn, "订单", other)

    def test_writes_are_not_committed(self):
        before = self.conn.total_changes
        create_view(self.conn, "订单", "本月", ViewConfig())
        self.assertGreater(self.conn.total_changes, before)
        self.conn.rollback()
        self.conn.close()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        self.assertEqual(len(self._rows()), 0)
