"""services/share.py 的导出与导入校验。"""

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from local_sql_manager.services.library import META_VERSION, open_library
from local_sql_manager.services.models import ShareError
from local_sql_manager.services.share import (
    FORMAT_VERSION,
    MAX_COMPRESSION_RATIO,
    MAX_UNCOMPRESSED_BYTES,
    export_lsmf,
    import_lsmf,
)


def make_lsmf(
    directory,
    *,
    format=FORMAT_VERSION,
    fill_mb=0,
    db_bytes=None,
    name="bomb.lsmf",
    extra_entries=(),
    manifest=None,
    compresslevel=1,
):
    """构造一个分享包；fill_mb 用零字节填充 db.sqlite 以模拟 zip bomb。"""
    path = Path(directory) / name
    text = (
        json.dumps(manifest, ensure_ascii=False)
        if manifest is not None
        else json.dumps({"format": format, "app_version": "1.0.0"})
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as zf:
        zf.writestr("manifest.json", text)
        if fill_mb:
            with zf.open("db.sqlite", "w") as writer:
                chunk = b"\0" * (1024 * 1024)
                for _ in range(fill_mb):
                    writer.write(chunk)
        else:
            payload = db_bytes if db_bytes is not None else bare_sqlite()
            zf.writestr("db.sqlite", payload)
        for entry_name, payload in extra_entries:
            zf.writestr(entry_name, payload)
    return path


def bare_sqlite():
    """返回一个只含空表的合法库文件字节串。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "empty.sqlite"
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE "_empty" ("id" INTEGER)')
        conn.commit()
        conn.close()
        return path.read_bytes()


def sqlite_bytes(path):
    return Path(path).read_bytes()


class BaseShareTestCase(unittest.TestCase):
    """准备一个带用户表、一条审计记录的库。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.lib_path = self.dir / "库.sqlite"
        self.lib = open_library(str(self.lib_path))
        self.opened = []
        conn = self.lib.conn
        conn.execute(
            'CREATE TABLE "订单" ('
            '"id" INTEGER PRIMARY KEY AUTOINCREMENT, '
            '"客户" TEXT, '
            '"金额" REAL)'
        )
        conn.executemany(
            'INSERT INTO "订单" ("客户", "金额") VALUES (?, ?)',
            [("甲", 10.0), ("乙", 20.5)],
        )
        conn.execute(
            'INSERT INTO "__lsm_table" '
            '("table_name", "status", "created_at") VALUES (?, ?, ?)',
            ("订单", "active", "2026-11-10T08:00:00Z"),
        )
        conn.execute(
            'INSERT INTO "__lsm_op" '
            '("ts", "source", "kind", "table_name", "payload_json") '
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-11-10T08:00:00Z", "web", "update", "订单", "{}"),
        )
        conn.commit()

    def tearDown(self):
        for lib in self.opened:
            lib.conn.close()
        self.lib.conn.close()
        self._tmp.cleanup()

    def open_imported(self, path):
        lib = open_library(str(path))
        self.opened.append(lib)
        return lib


class TestExport(BaseShareTestCase):
    def export(self, app_version="1.0.0"):
        return export_lsmf(self.lib.conn, self.lib_path, self.dir / "a.lsmf", app_version)

    def test_writes_manifest_and_single_db_entry(self):
        out = self.export()
        self.assertTrue(out.exists())
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(zf.namelist(), ["manifest.json", "db.sqlite"])

    def test_manifest_describes_package(self):
        out = self.export("2.3.4")
        with zipfile.ZipFile(out) as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        self.assertEqual(manifest["format"], FORMAT_VERSION)
        self.assertEqual(manifest["app_version"], "2.3.4")
        self.assertRegex(manifest["exported_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(manifest["tables"], [{"name": "订单", "row_count": 2}])

    def test_export_omits_audit_history(self):
        out = self.export()
        with zipfile.ZipFile(out) as zf:
            zf.extract("db.sqlite", self.dir / "peek")
        lib = self.open_imported(self.dir / "peek" / "db.sqlite")
        self.assertEqual(
            lib.conn.execute('SELECT COUNT(*) FROM "__lsm_op"').fetchone()[0], 0
        )
        self.assertEqual(
            lib.conn.execute('SELECT COUNT(*) FROM "__lsm_trash_col"').fetchone()[0], 0
        )
        self.assertEqual(
            lib.conn.execute('SELECT COUNT(*) FROM "订单"').fetchone()[0], 2
        )

    def test_export_does_not_touch_source(self):
        self.export()
        self.assertEqual(
            self.lib.conn.execute('SELECT COUNT(*) FROM "__lsm_op"').fetchone()[0], 1
        )

    def test_export_overwrites_existing_package(self):
        out = self.dir / "a.lsmf"
        out.write_bytes(b"stale")
        exported = self.export()
        with zipfile.ZipFile(exported) as zf:
            self.assertEqual(zf.namelist(), ["manifest.json", "db.sqlite"])

    def test_export_refuses_to_overwrite_database(self):
        with self.assertRaises(ShareError):
            export_lsmf(self.lib.conn, self.lib_path, self.lib_path, "1.0.0")
        self.assertEqual(self.lib.conn.execute('SELECT COUNT(*) FROM "订单"').fetchone()[0], 2)

    def test_export_defaults_next_to_database(self):
        out = export_lsmf(self.lib.conn, self.lib_path, None, "1.0.0")
        self.assertEqual(out, self.lib_path.with_suffix(".lsmf"))
        self.assertTrue(out.exists())


class TestImportRoundTrip(BaseShareTestCase):
    def test_round_trip(self):
        out = export_lsmf(self.lib.conn, self.lib_path, self.dir / "a.lsmf", "1.0.0")
        dest = import_lsmf(out, self.dir)
        self.assertTrue(Path(dest).exists())
        lib = self.open_imported(dest)
        self.assertEqual(
            lib.conn.execute('SELECT COUNT(*) FROM "__lsm_op"').fetchone()[0], 0
        )
        self.assertEqual(
            lib.conn.execute('SELECT COUNT(*) FROM "订单"').fetchone()[0], 2
        )
        self.assertEqual(
            lib.conn.execute(
                'SELECT "value" FROM "__lsm_meta" WHERE "key"=\'version\''
            ).fetchone()[0],
            str(META_VERSION),
        )

    def test_import_names_file_after_package(self):
        dest = import_lsmf(make_lsmf(self.dir, name="九月报表.lsmf"), self.dir)
        self.assertEqual(Path(dest).name, "九月报表.sqlite")

    def test_import_suffixes_duplicate_names(self):
        package = make_lsmf(self.dir, name="dup.lsmf")
        first = import_lsmf(package, self.dir)
        second = import_lsmf(package, self.dir)
        self.assertEqual(Path(first).name, "dup.sqlite")
        self.assertEqual(Path(second).name, "dup-2.sqlite")

    def test_import_creates_libraries_dir(self):
        dest = import_lsmf(make_lsmf(self.dir), self.dir / "新建目录")
        self.assertTrue(Path(dest).exists())

    def test_import_leaves_no_temporary_files(self):
        import_lsmf(make_lsmf(self.dir, name="clean.lsmf"), self.dir)
        leftovers = sorted(p.name for p in self.dir.iterdir() if p.name.startswith("."))
        self.assertEqual(leftovers, [])


class TestImportRejects(BaseShareTestCase):
    def test_rejects_path_traversal(self):
        bad = self.dir / "bad.lsmf"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../../evil.sqlite", b"x")
            zf.writestr("manifest.json", '{"format": 1}')
        with self.assertRaises(ShareError):
            import_lsmf(bad, self.dir)

    def test_rejects_nested_entry_name(self):
        package = make_lsmf(self.dir, extra_entries=(("sub/db.sqlite", b"x"),))
        with self.assertRaises(ShareError):
            import_lsmf(package, self.dir)

    def test_rejects_extra_entry(self):
        package = make_lsmf(self.dir, extra_entries=(("notes.txt", b"hi"),))
        with self.assertRaises(ShareError):
            import_lsmf(package, self.dir)

    def test_rejects_missing_manifest(self):
        bad = self.dir / "nomani.lsmf"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("db.sqlite", bare_sqlite())
        with self.assertRaises(ShareError):
            import_lsmf(bad, self.dir)

    def test_rejects_newer_manifest(self):
        with self.assertRaises(ShareError):
            import_lsmf(make_lsmf(self.dir, format=99), self.dir)

    def test_rejects_invalid_format_value(self):
        for value in ("1", 0, None, True):
            with self.subTest(value=value):
                package = make_lsmf(self.dir, manifest={"format": value})
                with self.assertRaises(ShareError):
                    import_lsmf(package, self.dir)

    def test_rejects_broken_manifest(self):
        package = make_lsmf(self.dir, manifest="not an object")
        with self.assertRaises(ShareError):
            import_lsmf(package, self.dir)

    def test_rejects_zip_bomb(self):
        with self.assertRaises(ShareError):
            import_lsmf(make_lsmf(self.dir, fill_mb=600), self.dir)
        self.assertLess(MAX_UNCOMPRESSED_BYTES, 600 * 1024 * 1024)

    def test_rejects_high_compression_ratio(self):
        package = make_lsmf(
            self.dir, fill_mb=20, name="ratio.lsmf", compresslevel=9
        )
        with self.assertRaises(ShareError):
            import_lsmf(package, self.dir)
        self.assertLess(MAX_COMPRESSION_RATIO, 1000)

    def test_rejects_non_zip_file(self):
        bad = self.dir / "bad.lsmf"
        bad.write_bytes(b"definitely not a zip")
        with self.assertRaises(ShareError):
            import_lsmf(bad, self.dir)

    def test_rejects_non_sqlite_payload(self):
        package = make_lsmf(self.dir, db_bytes=b"hello, world")
        with self.assertRaises(ShareError):
            import_lsmf(package, self.dir)

    def test_rejects_database_with_newer_meta_version(self):
        source = self.dir / "future.sqlite"
        conn = sqlite3.connect(source)
        conn.execute('CREATE TABLE "__lsm_meta" ("key" TEXT PRIMARY KEY, "value" TEXT)')
        conn.execute(
            'INSERT INTO "__lsm_meta" ("key", "value") VALUES (\'version\', ?)',
            (str(META_VERSION + 1),),
        )
        conn.commit()
        conn.close()
        package = make_lsmf(self.dir, db_bytes=sqlite_bytes(source))
        with self.assertRaises(ShareError):
            import_lsmf(package, self.dir)

    def test_rejection_leaves_nothing_behind(self):
        package = make_lsmf(self.dir, name="bad2.lsmf", db_bytes=b"hello")
        with self.assertRaises(ShareError):
            import_lsmf(package, self.dir)
        leftovers = sorted(
            p.name for p in self.dir.iterdir() if p.name != "bad2.lsmf"
        )
        self.assertNotIn("bad2.sqlite", leftovers)
        self.assertFalse([name for name in leftovers if name.startswith(".tmp-")])


if __name__ == "__main__":
    unittest.main()
