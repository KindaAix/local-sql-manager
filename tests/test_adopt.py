import sqlite3
import unittest
from unittest import mock

from local_sql_manager.services import adopt as adopt_service
from local_sql_manager.services.adopt import adopt_table, check_adoptable
from local_sql_manager.services.audit import get_op, history
from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.models import AdoptError, ViewConfig
from local_sql_manager.services.rows import get_page, restore_rows, soft_delete
from local_sql_manager.services.schema import (
    ColumnSpec,
    create_table,
    get_fields,
    list_tables,
    rename_table,
)


class BaseAdoptTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        ensure_meta(self.conn)

    def tearDown(self):
        self.conn.close()

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def columns(self, table):
        return [row["name"] for row in self.conn.execute(f'PRAGMA table_info("{table}")')]

    def index_names(self, table):
        return sorted(row["name"] for row in self.conn.execute(f'PRAGMA index_list("{table}")'))

    def object_sql(self, name):
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else row["sql"]

    def count(self, table):
        return self.conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    def table_exists(self, name):
        return (
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            is not None
        )


class TestCheckAdoptable(BaseAdoptTestCase):
    def test_no_pk_is_adoptable(self):
        self.execute('CREATE TABLE legacy ("名称" TEXT)')
        ok, reason = check_adoptable(self.conn, "legacy")
        self.assertTrue(ok)
        self.assertIn("id", reason)

    def test_text_id_rejected(self):
        self.execute('CREATE TABLE t ("id" TEXT PRIMARY KEY)')
        ok, reason = check_adoptable(self.conn, "t")
        self.assertFalse(ok)
        self.assertIn("主键", reason)

    def test_composite_pk_rejected(self):
        self.execute("CREATE TABLE c (a TEXT, b TEXT, PRIMARY KEY (a, b))")
        ok, reason = check_adoptable(self.conn, "c")
        self.assertFalse(ok)
        self.assertIn("主键", reason)

    def test_integer_pk_is_reused(self):
        self.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, v TEXT)")
        ok, reason = check_adoptable(self.conn, "p")
        self.assertTrue(ok)
        self.assertIn("复用", reason)

    def test_non_integer_id_without_pk_rejected(self):
        self.execute('CREATE TABLE t ("id" TEXT, "v" TEXT)')
        ok, reason = check_adoptable(self.conn, "t")
        self.assertFalse(ok)
        self.assertIn("id", reason)

    def test_integer_id_without_pk_rejected(self):
        self.execute('CREATE TABLE t ("id" INTEGER, "v" TEXT)')
        self.assertFalse(check_adoptable(self.conn, "t")[0])

    def test_view_rejected(self):
        self.execute('CREATE VIEW "报表" AS SELECT 1 AS "x"')
        ok, reason = check_adoptable(self.conn, "报表")
        self.assertFalse(ok)
        self.assertIn("视图", reason)

    def test_internal_table_rejected(self):
        self.assertFalse(check_adoptable(self.conn, "__lsm_meta")[0])

    def test_missing_table_rejected(self):
        ok, reason = check_adoptable(self.conn, "不存在")
        self.assertFalse(ok)
        self.assertIn("不存在", reason)

    def test_managed_table_is_not_adoptable_again(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        ok, reason = check_adoptable(self.conn, "订单")
        self.assertFalse(ok)
        self.assertIn("已启用", reason)

    def test_without_rowid_rejected(self):
        self.execute(
            "CREATE TABLE w (id INTEGER PRIMARY KEY, v TEXT) WITHOUT ROWID"
        )
        ok, reason = check_adoptable(self.conn, "w")
        self.assertFalse(ok)
        self.assertIn("WITHOUT ROWID", reason)

    def test_reason_mentions_unique_rewrite(self):
        self.execute('CREATE TABLE u (id INTEGER PRIMARY KEY, code TEXT UNIQUE)')
        ok, reason = check_adoptable(self.conn, "u")
        self.assertTrue(ok)
        self.assertIn("部分索引", reason)

    def test_rejects_generated_columns(self):
        self.execute(
            'CREATE TABLE g (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER AS (a * 2))'
        )
        ok, reason = check_adoptable(self.conn, "g")
        self.assertFalse(ok)
        self.assertIn("生成列", reason)


class TestAdoptTable(BaseAdoptTestCase):
    def test_no_pk_gets_new_id(self):
        self.execute('CREATE TABLE legacy ("名称" TEXT)')
        self.execute("INSERT INTO legacy VALUES ('a')")
        op = adopt_table(self.conn, "legacy")
        cols = self.columns("legacy")
        self.assertEqual(cols[:4], ["id", "created_at", "updated_at", "deleted_at"])
        self.assertEqual(self.count("legacy"), 1)
        self.assertEqual(self.execute('SELECT "名称" FROM legacy').fetchone()[0], "a")
        self.assertIsInstance(op, int)

    def test_new_aux_values_are_utc_and_deleted_at_null(self):
        self.execute('CREATE TABLE legacy (v TEXT)')
        self.execute("INSERT INTO legacy VALUES ('a')")
        adopt_table(self.conn, "legacy")
        row = self.execute("SELECT * FROM legacy").fetchone()
        self.assertRegex(row["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(row["created_at"], row["updated_at"])
        self.assertIsNone(row["deleted_at"])
        self.assertEqual(row["id"], 1)

    def test_reuses_integer_pk_without_duplicate_id(self):
        self.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, v TEXT)")
        self.execute("INSERT INTO p (id, v) VALUES (5, 'a')")
        adopt_table(self.conn, "p")
        self.assertEqual(self.columns("p"), ["id", "created_at", "updated_at", "deleted_at", "v"])
        self.assertEqual(self.execute("SELECT id FROM p").fetchone()[0], 5)
        self.assertEqual(self.count("p"), 1)

    def test_renames_single_integer_pk_to_id(self):
        self.execute('CREATE TABLE p ("序号" INTEGER PRIMARY KEY, v TEXT)')
        self.execute('INSERT INTO p ("序号", v) VALUES (7, "a")')
        op = adopt_table(self.conn, "p")
        self.assertEqual(self.columns("p")[:4], ["id", "created_at", "updated_at", "deleted_at"])
        self.assertEqual(self.execute("SELECT id FROM p").fetchone()[0], 7)
        self.assertEqual([f.name for f in get_fields(self.conn, "p")], ["v"])
        payload = get_op(self.conn, op)["payload"]
        self.assertEqual(payload["renamed_pk"], {"from": "序号", "to": "id"})

    def test_preserves_rows_and_defaults(self):
        self.execute(
            'CREATE TABLE d (id INTEGER PRIMARY KEY, v TEXT NOT NULL DEFAULT "x")'
        )
        self.execute("INSERT INTO d (id) VALUES (1)")
        self.execute("INSERT INTO d (id, v) VALUES (2, 'y')")
        adopt_table(self.conn, "d")
        self.assertEqual(
            self.execute('SELECT v FROM d ORDER BY id').fetchall()[0][0], "x"
        )
        self.assertEqual(self.count("d"), 2)
        self.execute(
            'INSERT INTO d (created_at, updated_at) VALUES ("t", "t")'
        )
        self.assertEqual(self.execute("SELECT v FROM d WHERE id = 3").fetchone()[0], "x")

    def test_unique_index_becomes_partial(self):
        self.execute('CREATE TABLE u (id INTEGER PRIMARY KEY, code TEXT UNIQUE)')
        adopt_table(self.conn, "u")
        row = self.execute(
            "SELECT sql FROM sqlite_master WHERE name LIKE '%code%'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("UNIQUE", row["sql"].upper())
        self.assertIn("WHERE deleted_at IS NULL", row["sql"])

    def test_partial_unique_index_keeps_original_condition(self):
        self.execute("CREATE TABLE q (id INTEGER PRIMARY KEY, code TEXT)")
        self.execute(
            'CREATE UNIQUE INDEX "q_code" ON "q" ("code") WHERE code IS NOT NULL'
        )
        adopt_table(self.conn, "q")
        sql = self.object_sql("q_code")
        self.assertIn("code IS NOT NULL", sql)
        self.assertIn("deleted_at IS NULL", sql)

    def test_restore_conflicting_unique_raises(self):
        self.execute('CREATE TABLE u (id INTEGER PRIMARY KEY, code TEXT UNIQUE)')
        adopt_table(self.conn, "u")
        self.execute("INSERT INTO u (code) VALUES ('x')")
        rid = self.execute("SELECT id FROM u").fetchone()[0]
        soft_delete(self.conn, "u", [rid])
        self.execute("INSERT INTO u (code) VALUES ('x')")
        with self.assertRaises(ValueError) as ctx:
            restore_rows(self.conn, "u", [rid])
        self.assertIn("唯一", str(ctx.exception))
        self.conn.rollback()

    def test_non_unique_index_is_preserved(self):
        self.execute("CREATE TABLE n (id INTEGER PRIMARY KEY, code TEXT)")
        self.execute('CREATE INDEX "n_code_idx" ON "n" ("code")')
        adopt_table(self.conn, "n")
        self.assertIn("n_code_idx", self.index_names("n"))

    def test_preserves_check_constraint(self):
        self.execute(
            "CREATE TABLE ck (id INTEGER PRIMARY KEY, amount REAL CHECK (amount >= 0))"
        )
        adopt_table(self.conn, "ck")
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute("INSERT INTO ck (amount) VALUES (-1)")

    def test_check_constraint_follows_renamed_pk(self):
        self.execute(
            'CREATE TABLE ck ("序号" INTEGER PRIMARY KEY, amount REAL,'
            ' CHECK ("序号" > 0 AND amount >= 0))'
        )
        adopt_table(self.conn, "ck")
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute("INSERT INTO ck (amount) VALUES (-1)")

    def test_preserves_foreign_key_on_child_table(self):
        self.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, v TEXT)")
        self.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, pid INTEGER"
            " REFERENCES parent(id) ON DELETE CASCADE)"
        )
        adopt_table(self.conn, "child")
        rows = self.execute("PRAGMA foreign_key_list(child)").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["table"], "parent")
        self.assertEqual(rows[0]["from"], "pid")
        self.assertEqual(rows[0]["to"], "id")
        self.assertEqual(rows[0]["on_delete"], "CASCADE")

    def test_self_reference_follows_renamed_pk(self):
        self.execute(
            'CREATE TABLE t ("序号" INTEGER PRIMARY KEY, boss INTEGER REFERENCES t("序号"))'
        )
        adopt_table(self.conn, "t")
        rows = self.execute("PRAGMA foreign_key_list(t)").fetchall()
        self.assertEqual(rows[0]["table"], "t")
        self.assertEqual(rows[0]["from"], "boss")
        self.assertEqual(rows[0]["to"], "id")

    def test_trigger_is_recreated(self):
        self.execute("CREATE TABLE tr (id INTEGER PRIMARY KEY, v TEXT)")
        self.execute("CREATE TABLE log (v TEXT)")
        self.execute(
            "CREATE TRIGGER tr_ai AFTER INSERT ON tr BEGIN"
            " INSERT INTO log (v) VALUES (new.v); END"
        )
        adopt_table(self.conn, "tr")
        self.execute("INSERT INTO tr (v) VALUES ('x')")
        self.assertEqual(self.execute("SELECT v FROM log").fetchone()[0], "x")

    def test_existing_aux_columns_are_kept(self):
        self.execute(
            'CREATE TABLE e (id INTEGER PRIMARY KEY, created_at TEXT, updated_at TEXT,'
            ' deleted_at TEXT, v TEXT)'
        )
        self.execute(
            "INSERT INTO e (id, created_at, updated_at, deleted_at, v)"
            " VALUES (1, '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z', NULL, 'a')"
        )
        op = adopt_table(self.conn, "e")
        self.assertEqual(
            self.columns("e"), ["id", "created_at", "updated_at", "deleted_at", "v"]
        )
        row = self.execute("SELECT * FROM e").fetchone()
        self.assertEqual(row["created_at"], "2020-01-01T00:00:00Z")
        self.assertEqual(get_op(self.conn, op)["payload"]["added_columns"], [])

    def test_null_created_at_is_backfilled(self):
        self.execute("CREATE TABLE e (id INTEGER PRIMARY KEY, created_at TEXT, v TEXT)")
        self.execute("INSERT INTO e (id, v) VALUES (1, 'a')")
        adopt_table(self.conn, "e")
        row = self.execute("SELECT * FROM e").fetchone()
        self.assertRegex(row["created_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_registers_metadata_and_fields(self):
        self.execute('CREATE TABLE legacy ("名称" TEXT, "金额" REAL)')
        adopt_table(self.conn, "legacy")
        self.assertEqual([f.name for f in get_fields(self.conn, "legacy")], ["名称", "金额"])
        row = self.execute(
            'SELECT status FROM "__lsm_table" WHERE "table_name" = ?', ("legacy",)
        ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertIn("__lsm_idx_6_legacy_2_名称", self.index_names("legacy"))

    def test_records_adopt_audit(self):
        self.execute('CREATE TABLE u (id INTEGER PRIMARY KEY, code TEXT UNIQUE)')
        op = adopt_table(self.conn, "u")
        record = get_op(self.conn, op)
        self.assertEqual(record["kind"], "adopt")
        self.assertEqual(record["table"], "u")
        self.assertEqual(record["source"], "web")
        self.assertEqual(record["irreversible"], 1)
        payload = record["payload"]
        self.assertEqual(payload["name"], "u")
        self.assertEqual(
            payload["added_columns"], ["created_at", "updated_at", "deleted_at"]
        )
        self.assertEqual(len(payload["rewritten_indexes"]), 1)
        self.assertEqual(payload["rewritten_indexes"][0]["columns"], ["code"])

    def test_failure_rolls_back_and_leaves_no_temp_table(self):
        self.execute('CREATE TABLE legacy ("名称" TEXT)')
        self.execute("INSERT INTO legacy VALUES ('a')")
        self.conn.commit()
        with mock.patch.object(
            adopt_service, "_create_indexes", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                adopt_table(self.conn, "legacy")
        self.assertEqual(self.columns("legacy"), ["名称"])
        self.assertFalse(self.table_exists(adopt_service.TEMP_TABLE))
        self.assertEqual(history(self.conn, "legacy"), [])

    def test_does_not_commit(self):
        self.execute('CREATE TABLE legacy ("名称" TEXT)')
        self.conn.commit()
        adopt_table(self.conn, "legacy")
        self.conn.rollback()
        self.assertEqual(self.columns("legacy"), ["名称"])
        self.assertEqual(history(self.conn, "legacy"), [])
        self.assertEqual(self.execute('SELECT COUNT(*) FROM "__lsm_table"').fetchone()[0], 0)

    def test_joins_outer_transaction(self):
        self.execute('CREATE TABLE legacy ("名称" TEXT)')
        self.conn.commit()
        self.conn.execute("BEGIN")
        adopt_table(self.conn, "legacy")
        self.conn.rollback()
        self.assertEqual(self.columns("legacy"), ["名称"])

    def test_foreign_keys_disabled_and_restored(self):
        self.execute("PRAGMA foreign_keys=ON")
        self.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, v TEXT)")
        self.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id))"
        )
        self.execute("INSERT INTO parent (id, v) VALUES (1, 'a')")
        self.execute("INSERT INTO child (id, pid) VALUES (1, 1)")
        self.conn.commit()
        adopt_table(self.conn, "parent")
        self.assertEqual(self.count("child"), 1)
        self.assertEqual(
            self.execute("PRAGMA foreign_key_check").fetchall(), []
        )
        self.assertEqual(self.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_failure_restores_foreign_keys(self):
        self.execute("PRAGMA foreign_keys=ON")
        self.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, v TEXT)")
        self.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id))"
        )
        self.conn.commit()
        with mock.patch.object(
            adopt_service, "_create_indexes", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                adopt_table(self.conn, "parent")
        self.assertEqual(self.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.columns("parent"), ["id", "v"])
        self.assertFalse(self.table_exists(adopt_service.TEMP_TABLE))

    def test_uppercase_id_pk_is_reused(self):
        self.execute('CREATE TABLE p ("ID" INTEGER PRIMARY KEY, v TEXT)')
        self.execute('INSERT INTO p ("ID", v) VALUES (3, "a")')
        adopt_table(self.conn, "p")
        self.assertEqual(self.columns("p")[:4], ["id", "created_at", "updated_at", "deleted_at"])
        self.assertEqual(self.execute("SELECT id FROM p").fetchone()[0], 3)

    def test_uppercase_aux_columns_are_canonicalized(self):
        self.execute(
            'CREATE TABLE e (id INTEGER PRIMARY KEY, "Created_At" TEXT,'
            ' "Updated_At" TEXT, "Deleted_At" TEXT, v TEXT)'
        )
        self.execute(
            'INSERT INTO e (id, "Created_At", "Updated_At", "Deleted_At", v)'
            " VALUES (1, '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z', NULL, 'a')"
        )
        adopt_table(self.conn, "e")
        self.assertEqual(
            self.columns("e"), ["id", "created_at", "updated_at", "deleted_at", "v"]
        )
        self.assertEqual(
            self.execute("SELECT created_at FROM e").fetchone()[0],
            "2020-01-01T00:00:00Z",
        )

    def test_existing_deleted_at_not_null_is_made_nullable(self):
        self.execute(
            "CREATE TABLE e (id INTEGER PRIMARY KEY, deleted_at TEXT NOT NULL"
            " DEFAULT '', v TEXT)"
        )
        self.execute("INSERT INTO e (id, deleted_at, v) VALUES (1, '', 'a')")
        adopt_table(self.conn, "e")
        soft_delete(self.conn, "e", [1])
        restore_rows(self.conn, "e", [1])
        self.assertIsNone(
            self.execute("SELECT deleted_at FROM e WHERE id = 1").fetchone()[0]
        )

    def test_standalone_table_adopts_inside_outer_transaction_with_fk_on(self):
        self.execute("PRAGMA foreign_keys=ON")
        self.execute("CREATE TABLE free (id INTEGER PRIMARY KEY, v TEXT)")
        self.conn.commit()
        self.conn.execute("BEGIN")
        adopt_table(self.conn, "free")
        self.conn.rollback()
        self.assertEqual(self.columns("free"), ["id", "v"])
        self.assertEqual(self.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_accepts_table_name_case_insensitively(self):
        self.execute('CREATE TABLE "Legacy" ("v" TEXT)')
        adopt_table(self.conn, "legacy")
        self.assertEqual(self.columns("Legacy")[:4], ["id", "created_at", "updated_at", "deleted_at"])

    def test_rejects_non_adoptable(self):
        self.execute('CREATE TABLE t ("id" TEXT PRIMARY KEY)')
        with self.assertRaises(AdoptError):
            adopt_table(self.conn, "t")


class _NoSavepointConnection:
    """代理连接：清理阶段对 ROLLBACK TO 抛 no such savepoint。"""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, parameters=()):
        if sql.strip().upper().startswith("ROLLBACK TO"):
            raise sqlite3.OperationalError("no such savepoint")
        return self._conn.execute(sql, parameters)


class TestReviewFixes(BaseAdoptTestCase):
    def test_sentinel_deleted_at_is_normalized(self):
        self.execute(
            "CREATE TABLE s (id INTEGER PRIMARY KEY,"
            " deleted_at TEXT NOT NULL DEFAULT '', v TEXT)"
        )
        self.execute("INSERT INTO s (id, deleted_at, v) VALUES (1, '', 'a')")
        self.execute("INSERT INTO s (id, deleted_at, v) VALUES (2, '0', 'b')")
        self.execute("INSERT INTO s (id, deleted_at, v) VALUES (3, 'FALSE', 'c')")
        ok, reason = check_adoptable(self.conn, "s")
        self.assertTrue(ok)
        self.assertIn("3 行", reason)
        self.assertIn("未删除", reason)

        adopt_table(self.conn, "s")
        self.assertEqual(get_page(self.conn, "s", ViewConfig()).total, 3)
        values = [
            row[0]
            for row in self.execute("SELECT deleted_at FROM s").fetchall()
        ]
        self.assertEqual(values, [None, None, None])
        info = {
            row["name"]: row for row in self.execute('PRAGMA table_info("s")')
        }
        self.assertIsNone(info["deleted_at"]["dflt_value"])

        self.execute("INSERT INTO s (v) VALUES ('d')")
        self.assertEqual(get_page(self.conn, "s", ViewConfig()).total, 4)

    def test_sentinel_integer_zero_is_normalized(self):
        self.execute(
            "CREATE TABLE s (id INTEGER PRIMARY KEY, deleted_at INTEGER DEFAULT 0, v TEXT)"
        )
        self.execute("INSERT INTO s (id, deleted_at, v) VALUES (1, 0, 'a')")
        adopt_table(self.conn, "s")
        self.assertIsNone(
            self.execute("SELECT deleted_at FROM s WHERE id = 1").fetchone()[0]
        )
        self.assertEqual(get_page(self.conn, "s", ViewConfig()).total, 1)

    def test_adopt_survives_views_and_foreign_table_triggers(self):
        self.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        self.execute("INSERT INTO t (v) VALUES ('a')")
        self.execute("CREATE VIEW t_view AS SELECT * FROM t")
        self.execute("CREATE TABLE other (x TEXT)")
        self.execute(
            "CREATE TRIGGER other_ai AFTER INSERT ON other"
            " BEGIN INSERT INTO t (v) VALUES (new.x); END"
        )
        adopt_table(self.conn, "t")
        rows = self.execute("SELECT v FROM t_view").fetchall()
        self.assertEqual([row[0] for row in rows], ["a"])
        self.execute("INSERT INTO other (x) VALUES ('b')")
        self.assertEqual(self.execute("SELECT COUNT(*) FROM t").fetchone()[0], 2)
        self.assertEqual(self.execute("PRAGMA legacy_alter_table").fetchone()[0], 0)

    def test_adopt_survives_unrelated_broken_view(self):
        self.execute("CREATE TABLE free (id INTEGER PRIMARY KEY, v TEXT)")
        self.execute("CREATE VIEW broken AS SELECT * FROM nope")
        adopt_table(self.conn, "free")
        self.assertEqual(
            self.columns("free"),
            ["id", "created_at", "updated_at", "deleted_at", "v"],
        )

    def test_rename_helper_restores_legacy_pragma_on_failure(self):
        self.execute("PRAGMA legacy_alter_table=1")
        with self.assertRaises(sqlite3.OperationalError):
            adopt_service._rename_table_strict(self.conn, "不存在", "x")
        self.assertEqual(self.execute("PRAGMA legacy_alter_table").fetchone()[0], 1)

    def test_schema_rename_updates_view_reference(self):
        self.execute("CREATE TABLE t (a TEXT)")
        self.execute("CREATE VIEW v AS SELECT a FROM t")
        self.execute("INSERT INTO t (a) VALUES ('x')")
        rename_table(self.conn, "t", "t2")
        self.assertEqual(self.execute("SELECT a FROM v").fetchone()[0], "x")
        self.assertIn("t2", self.object_sql("v"))
        self.assertEqual(self.execute("PRAGMA legacy_alter_table").fetchone()[0], 0)

    def test_schema_rename_works_with_broken_view(self):
        self.execute("CREATE TABLE t (a TEXT)")
        self.execute("CREATE VIEW broken AS SELECT * FROM nope")
        rename_table(self.conn, "t", "t2")
        self.assertIn("t2", list_tables(self.conn))
        self.assertEqual(self.execute("PRAGMA legacy_alter_table").fetchone()[0], 0)

    def test_savepoint_cleanup_failure_does_not_mask_original_error(self):
        self.execute("PRAGMA foreign_keys=ON")
        self.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, v TEXT)")
        self.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, pid INTEGER REFERENCES parent(id))"
        )
        self.conn.commit()
        proxy = _NoSavepointConnection(self.conn)
        with mock.patch.object(
            adopt_service, "_create_indexes", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                adopt_table(proxy, "parent")
        self.assertEqual(self.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.columns("parent"), ["id", "v"])
        self.assertFalse(self.table_exists(adopt_service.TEMP_TABLE))

    def test_unique_collation_is_preserved(self):
        self.execute(
            "CREATE TABLE u (id INTEGER PRIMARY KEY, code TEXT COLLATE NOCASE UNIQUE)"
        )
        adopt_table(self.conn, "u")
        self.execute("INSERT INTO u (code) VALUES ('A')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute("INSERT INTO u (code) VALUES ('a')")

    def test_unique_desc_is_preserved(self):
        self.execute(
            "CREATE TABLE u (id INTEGER PRIMARY KEY, code TEXT, UNIQUE (code DESC))"
        )
        adopt_table(self.conn, "u")
        row = self.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index'"
            " AND name LIKE '__lsm_uniq_%'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("DESC", row[0])

    def test_reason_mentions_on_conflict(self):
        self.execute(
            "CREATE TABLE u (id INTEGER PRIMARY KEY, code TEXT UNIQUE ON CONFLICT IGNORE)"
        )
        ok, reason = check_adoptable(self.conn, "u")
        self.assertTrue(ok)
        self.assertIn("ON CONFLICT", reason)

    def test_missing_metadata_rolls_back_rebuild(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_meta(conn)
        conn.execute('DROP TABLE "__lsm_table"')
        conn.execute('CREATE TABLE legacy ("名称" TEXT)')
        conn.execute("INSERT INTO legacy VALUES ('a')")
        with self.assertRaises(AdoptError):
            adopt_table(conn, "legacy")
        cols = [row["name"] for row in conn.execute('PRAGMA table_info("legacy")')]
        self.assertEqual(cols, ["名称"])
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ?",
                (adopt_service.TEMP_TABLE,),
            ).fetchone()
        )
        conn.close()

    def test_registered_trashed_table_is_not_adoptable(self):
        self.execute("CREATE TABLE legacy (v TEXT)")
        self.execute(
            'INSERT INTO "__lsm_table" ("table_name", "status", "created_at")'
            " VALUES (?, ?, ?)",
            ("legacy", "trashed", "2020-01-01T00:00:00Z"),
        )
        ok, reason = check_adoptable(self.conn, "legacy")
        self.assertFalse(ok)
        self.assertIn("已启用", reason)


if __name__ == "__main__":
    unittest.main()
