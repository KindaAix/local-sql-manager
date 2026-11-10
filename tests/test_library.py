import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from local_sql_manager.services import library as library_module
from local_sql_manager.services.library import (
    META_VERSION,
    ensure_meta,
    meta_version,
    open_library,
)
from local_sql_manager.services.models import UnsupportedFormat


class _NoWalConnection:
    """把 journal_mode 那条 PRAGMA 变成失败，模拟 -shm 建不出来的文件系统。"""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def execute(self, sql, *args):
        # 只让「设置」失败，读取 journal_mode 的断言仍要能走真实连接。
        if "journal_mode" in sql.lower() and "=" in sql:
            raise sqlite3.OperationalError("unable to open database file")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


class TestWalFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "共享盘.sqlite")
        self.real_connect = library_module._connect

    def test_open_succeeds_without_wal_when_shm_unavailable(self):
        opened = []

        def connect(path):
            conn = _NoWalConnection(self.real_connect(path))
            opened.append(conn)
            return conn

        with mock.patch.object(library_module, "_connect", side_effect=connect):
            with self.assertLogs(
                "local_sql_manager.services.library", level="WARNING"
            ) as logs:
                lib = open_library(self.path)
        self.addCleanup(lib.conn.close)

        self.assertTrue(any("WAL" in line for line in logs.output))
        mode = lib.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertNotEqual(mode.lower(), "wal")
        # 回落之后库照常可用：元数据表和普通读写都在。
        self.assertEqual(meta_version(lib.conn), META_VERSION)
        lib.conn.execute('CREATE TABLE "t" ("id" INTEGER PRIMARY KEY)')
        lib.conn.commit()
        self.assertEqual(lib.conn.execute('SELECT COUNT(*) FROM "t"').fetchone()[0], 0)


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

    def audit_indexes(self, conn):
        return {
            row[1]
            for row in conn.execute('PRAGMA index_list("__lsm_op")')
        }

    def test_audit_table_indexes(self):
        lib = self.open()
        self.assertTrue(
            {"__lsm_op_ts", "__lsm_op_table_ts"} <= self.audit_indexes(lib.conn)
        )

    def test_ensure_meta_adds_missing_indexes(self):
        lib = self.open()
        lib.conn.execute('DROP INDEX "__lsm_op_ts"')
        lib.conn.execute('DROP INDEX "__lsm_op_table_ts"')
        ensure_meta(lib.conn)
        self.assertTrue(
            {"__lsm_op_ts", "__lsm_op_table_ts"} <= self.audit_indexes(lib.conn)
        )

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
