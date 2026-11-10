import unittest

from local_sql_manager.services.types import TYPES, cast_expr, coerce, infer_type


class TestInferType(unittest.TestCase):
    def test_leading_zero_is_text(self):
        self.assertEqual(infer_type(["00123", "00456"]), ("text", False))

    def test_numbers_and_suspicious(self):
        self.assertEqual(infer_type(["1", "2", "3"]), ("number", False))
        self.assertEqual(infer_type(["1", "2", "abc"]), ("text", True))

    def test_dates_and_checkbox(self):
        self.assertEqual(infer_type(["2026-01-02", "2026-03-04"])[0], "date")
        self.assertEqual(infer_type(["是", "否", "是"])[0], "checkbox")

    def test_empty_values_are_ignored(self):
        self.assertEqual(infer_type(["", "1", ""]), ("number", False))
        self.assertEqual(infer_type(["", ""]), ("text", False))

    def test_plain_text_is_not_suspicious(self):
        self.assertEqual(infer_type(["北京", "上海"]), ("text", False))

    def test_decimals_are_numbers(self):
        self.assertEqual(infer_type(["1.5", "2.25"]), ("number", False))

    def test_datetime_with_time_is_text(self):
        self.assertEqual(
            infer_type(["2026-01-02 10:00:00"]), ("text", False)
        )
        self.assertEqual(
            infer_type(["2026-01-02T10:00:00Z"]), ("text", False)
        )

    def test_compact_digits_are_not_dates(self):
        self.assertEqual(infer_type(["20260102"]), ("number", False))

    def test_mixed_date_and_datetime_is_suspicious(self):
        self.assertEqual(
            infer_type(["2026-01-02", "2026-01-02 10:00:00"]), ("text", True)
        )

    def test_invalid_calendar_date_is_text(self):
        self.assertEqual(infer_type(["2026-02-30"]), ("text", False))

    def test_boolean_words_are_checkbox(self):
        self.assertEqual(infer_type(["TRUE", "false"]), ("checkbox", False))


class TestCastExpr(unittest.TestCase):
    def test_cast_expr(self):
        self.assertEqual(cast_expr("金额", "number"), 'CAST("金额" AS REAL)')

    def test_quotes_column(self):
        self.assertEqual(cast_expr('a"b', "number"), 'CAST("a""b" AS REAL)')

    def test_type_mapping(self):
        self.assertEqual(cast_expr("完成", "checkbox"), 'CAST("完成" AS INTEGER)')
        self.assertEqual(cast_expr("名称", "text"), 'CAST("名称" AS TEXT)')


class TestCoerce(unittest.TestCase):
    def test_number(self):
        self.assertEqual(coerce("12", "number"), 12)
        self.assertEqual(coerce("1.5", "number"), 1.5)
        self.assertIsNone(coerce("", "number"))

    def test_checkbox(self):
        self.assertEqual(coerce("是", "checkbox"), 1)
        self.assertEqual(coerce("FALSE", "checkbox"), 0)
        self.assertIsNone(coerce("", "checkbox"))
        with self.assertRaises(ValueError):
            coerce("也许", "checkbox")

    def test_multiselect(self):
        self.assertEqual(coerce(["a", "b"], "multiselect"), '["a", "b"]')

    def test_date_normalizes(self):
        self.assertEqual(coerce("2026-1-2", "date"), "2026-01-02")

    def test_text(self):
        self.assertEqual(coerce("  北京  ", "text"), "北京")
        self.assertIsNone(coerce("", "text"))

    def test_types_list(self):
        self.assertEqual(
            set(TYPES),
            {"text", "number", "date", "checkbox", "select", "multiselect", "url"},
        )


if __name__ == "__main__":
    unittest.main()
