import os
import sqlite3
import tempfile
import unittest

from local_sql_manager.services.library import (
    META_VERSION,
    ensure_meta,
    meta_version,
    open_library,
)
from local_sql_manager.services.models import UnsupportedFormat


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "测试.sqlite")
        self.opened = []

    def tearDown(self):
        for lib in self.opened:
            lib.conn.close()
        self.tmp.cleanup()

    def open(self, path=None):
        lib = open_library(path or self.path)
        self.opened.append(lib)
        return lib

    def table_names(self):
        conn = sqlite3.connect(self.path)
        try:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()

    def test_creates_meta_tables(self):
        lib = self.open()
        names = {
            r[0]
            for r in lib.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(
            {
                "__lsm_meta",
                "__lsm_table",
                "__lsm_field",
                "__lsm_view",
                "__lsm_op",
                "__lsm_trash_col",
            }
            <= names
        )
        self.assertEqual(meta_version(lib.conn), META_VERSION)

    def test_rejects_newer_format(self):
        lib = self.open()
        lib.conn.execute(
            'UPDATE "__lsm_meta" SET "value"=\'9\' WHERE "key"=\'version\''
        )
        lib.conn.commit()
        self.opened.remove(lib)
        lib.conn.close()
        with self.assertRaises(UnsupportedFormat):
            open_library(self.path)

    def test_rejects_newer_format_without_creating_tables(self):
        conn = sqlite3.connect(self.path)
        conn.execute(
            'CREATE TABLE "__lsm_meta" ("key" TEXT PRIMARY KEY, "value" TEXT)'
        )
        conn.execute(
            'INSERT INTO "__lsm_meta" ("key", "value") VALUES (\'version\', ?)',
            (str(META_VERSION + 1),),
        )
        conn.commit()
        conn.close()

        with self.assertRaises(UnsupportedFormat):
            open_library(self.path)

        self.assertEqual(self.table_names(), {"__lsm_meta"})

    def test_rejects_non_integer_version(self):
        lib = self.open()
        lib.conn.execute(
            'UPDATE "__lsm_meta" SET "value"=\'abc\' WHERE "key"=\'version\''
        )
        lib.conn.commit()
        self.opened.remove(lib)
        lib.conn.close()
        with self.assertRaises(UnsupportedFormat):
            open_library(self.path)

    def test_ensure_meta_corrects_stale_version(self):
        lib = self.open()
        lib.conn.execute(
            'UPDATE "__lsm_meta" SET "value"=\'0\' WHERE "key"=\'version\''
        )
        lib.conn.commit()
        ensure_meta(lib.conn)
        self.assertEqual(meta_version(lib.conn), META_VERSION)

    def test_connection_settings(self):
        lib = self.open()
        self.assertEqual(lib.path, self.path)
        self.assertIs(lib.conn.row_factory, sqlite3.Row)
        self.assertEqual(lib.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_ensure_meta_is_idempotent(self):
        lib = self.open()
        ensure_meta(lib.conn)
        ensure_meta(lib.conn)
        count = lib.conn.execute(
            'SELECT COUNT(*) FROM "__lsm_meta" WHERE "key"=\'version\''
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_plain_sqlite_file_gets_meta(self):
        conn = sqlite3.connect(self.path)
        conn.execute('CREATE TABLE "订单" ("id" INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()
        lib = self.open()
        self.assertEqual(meta_version(lib.conn), META_VERSION)


if __name__ == "__main__":
    unittest.main()
