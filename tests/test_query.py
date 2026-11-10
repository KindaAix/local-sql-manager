import unittest

from local_sql_manager.services.models import Field, ViewConfig
from local_sql_manager.services.query import OPS, build_order, build_where, page_sql


class TestBuildWhere(unittest.TestCase):
    def test_text_filter_uses_param(self):
        sql, params = build_where(
            [{"column": "客户", "op": "contains", "value": "云栖"}],
            [Field("客户", "text")],
        )
        self.assertIn("LIKE ?", sql)
        self.assertEqual(params, ["%云栖%"])

    def test_number_filter_casts(self):
        sql, params = build_where(
            [{"column": "金额", "op": "gte", "value": "100"}],
            [Field("金额", "number")],
        )
        self.assertIn('CAST("金额" AS REAL) >= ?', sql)
        self.assertEqual(params, [100.0])

    def test_unknown_column_ignored(self):
        self.assertEqual(
            build_where([{"column": "不存在", "op": "eq", "value": 1}], []),
            ("", []),
        )

    def test_empty_filters(self):
        self.assertEqual(build_where([], [Field("金额", "number")]), ("", []))
        self.assertEqual(build_where(None, []), ("", []))

    def test_date_filter_uses_substr(self):
        sql, params = build_where(
            [{"column": "截止日期", "op": "gte", "value": "2026-01-01T10:00:00Z"}],
            [Field("截止日期", "date")],
        )
        self.assertIn('substr("截止日期", 1, 10) >= ?', sql)
        self.assertEqual(params, ["2026-01-01"])

    def test_checkbox_filter(self):
        sql, params = build_where(
            [{"column": "已完成", "op": "is_true"}], [Field("已完成", "checkbox")]
        )
        self.assertIn('CAST("已完成" AS INTEGER) = 1', sql)
        self.assertEqual(params, [])

    def test_contains_escapes_wildcards(self):
        _, params = build_where(
            [{"column": "客户", "op": "contains", "value": "100%_"}],
            [Field("客户", "text")],
        )
        self.assertEqual(params, ["%100\\%\\_%"])

    def test_invalid_op_ignored(self):
        self.assertEqual(
            build_where(
                [{"column": "金额", "op": "contains", "value": "1"}],
                [Field("金额", "number")],
            ),
            ("", []),
        )

    def test_blank_value_ignored_except_empty_ops(self):
        self.assertEqual(
            build_where(
                [{"column": "客户", "op": "contains", "value": ""}],
                [Field("客户", "text")],
            ),
            ("", []),
        )
        sql, _ = build_where(
            [{"column": "客户", "op": "empty", "value": ""}],
            [Field("客户", "text")],
        )
        self.assertIn("TRIM", sql)

    def test_multiple_filters_joined_with_and(self):
        sql, params = build_where(
            [
                {"column": "客户", "op": "contains", "value": "云"},
                {"column": "金额", "op": "gt", "value": 10},
            ],
            [Field("客户", "text"), Field("金额", "number")],
        )
        self.assertEqual(sql.count(" AND "), 1)
        self.assertEqual(params, ["%云%", 10.0])


class TestBuildOrder(unittest.TestCase):
    def test_order_quotes_column_and_defaults_asc(self):
        self.assertEqual(build_order([{"column": "金额"}], None), '"金额" ASC')

    def test_order_desc_and_multiple(self):
        self.assertEqual(
            build_order([{"column": "金额", "dir": "desc"}, {"column": "客户"}], None),
            '"金额" DESC, "客户" ASC',
        )

    def test_group_column_comes_first(self):
        self.assertEqual(
            build_order([{"column": "金额", "dir": "desc"}], {"column": "状态"}),
            '"状态" ASC, "金额" DESC',
        )

    def test_empty_order(self):
        self.assertEqual(build_order([], None), "")
        self.assertEqual(build_order(None, None), "")

    def test_invalid_direction_defaults_asc(self):
        self.assertEqual(
            build_order([{"column": "金额", "dir": "DROP TABLE"}], None),
            '"金额" ASC',
        )


class TestPageSql(unittest.TestCase):
    def test_page_sql_filters_soft_deleted_and_binds(self):
        sql, params = page_sql(
            "订单",
            [Field("金额", "number")],
            ViewConfig(filters=[{"column": "金额", "op": "gte", "value": 5}]),
            limit=100,
            offset=200,
        )
        self.assertTrue(sql.startswith('SELECT * FROM "订单"'))
        self.assertIn('"deleted_at" IS NULL', sql)
        self.assertIn('CAST("金额" AS REAL) >= ?', sql)
        self.assertIn('ORDER BY "id" ASC', sql)
        self.assertIn("LIMIT ? OFFSET ?", sql)
        self.assertEqual(params, [5.0, 100, 200])

    def test_page_sql_without_soft_delete(self):
        sql, _ = page_sql(
            "legacy",
            [Field("名称", "text")],
            ViewConfig(),
            limit=10,
            offset=0,
            soft_delete=False,
        )
        self.assertNotIn("deleted_at", sql)

    def test_page_sql_puts_group_in_order(self):
        sql, _ = page_sql(
            "订单",
            [Field("状态", "select")],
            ViewConfig(group={"column": "状态"}),
            limit=10,
            offset=0,
        )
        self.assertIn('ORDER BY "状态" ASC, "id" ASC', sql)

    def test_page_sql_ignores_sort_on_unknown_column(self):
        sql, _ = page_sql(
            "订单",
            [Field("金额", "number")],
            ViewConfig(sorts=[{"column": "没有", "dir": "asc"}]),
            limit=10,
            offset=0,
        )
        self.assertIn('ORDER BY "id" ASC', sql)
        self.assertNotIn("没有", sql)

    def test_ops_cover_expected_operators(self):
        self.assertIn("contains", OPS["text"])
        self.assertIn("gte", OPS["number"])
        self.assertIn("is_true", OPS["checkbox"])


if __name__ == "__main__":
    unittest.main()
