import unittest

from local_sql_manager.services.models import (
    AdoptError,
    ConflictError,
    Field,
    Group,
    Irreversible,
    PageResult,
    ShareError,
    StaleOperation,
    UnsupportedFormat,
    ViewConfig,
)
from local_sql_manager.services.util import utcnow_iso


class TestUtil(unittest.TestCase):
    def test_utcnow_format(self):
        self.assertRegex(utcnow_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestViewConfig(unittest.TestCase):
    def test_defaults_are_independent(self):
        a, b = ViewConfig(), ViewConfig()
        a.filters.append({"column": "x"})
        self.assertEqual(b.filters, [])
        a.widths["x"] = 200
        self.assertEqual(b.widths, {})


class TestModels(unittest.TestCase):
    def test_field_defaults(self):
        f = Field("金额", "number")
        self.assertEqual((f.options, f.position, f.width), ((), 0, 140))

    def test_page_result_defaults(self):
        page = PageResult(rows=[], total=0)
        self.assertEqual((page.groups, page.page, page.page_size), ((), 1, 100))

    def test_group(self):
        g = Group("已完成", 3, (("金额", 120.0),))
        self.assertEqual((g.key, g.count, g.aggregates), ("已完成", 3, (("金额", 120.0),)))

    def test_conflict_error_carries_rows(self):
        err = ConflictError([{"id": 1}])
        self.assertEqual(err.rows, [{"id": 1}])
        for exc in (StaleOperation, Irreversible, ShareError, AdoptError, UnsupportedFormat):
            self.assertTrue(issubclass(exc, Exception))


if __name__ == "__main__":
    unittest.main()
