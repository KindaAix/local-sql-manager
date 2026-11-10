import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime

from local_sql_manager.services import audit
from local_sql_manager.services.import_excel import (
    _header_names,
    _sanitize_table_name,
    import_sheets,
    parse_workbook,
)
from local_sql_manager.services.library import ensure_meta
from local_sql_manager.services.schema import (
    ColumnSpec,
    create_table,
    get_fields,
    list_tables,
    list_trashed_tables,
)
from tests.fixtures.make_fixtures import (
    make_bad_workbook,
    make_csv,
    make_csv_bom_dup,
    make_dup_workbook,
    make_large_workbook,
    make_reserved_workbook,
    make_workbook,
)


class ImportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        ensure_meta(self.conn)

    def make(self, name, maker):
        target = os.path.join(self.tmp.name, name)
        maker(target)
        return target

    def rows(self, table):
        return self.conn.execute(f'SELECT * FROM "{table}"').fetchall()


class TestParseWorkbook(ImportTestCase):
    def test_preview_marks_empty_and_types(self):
        previews = parse_workbook(self.make("订单.xlsx", make_workbook))
        self.assertEqual([p.name for p in previews], ["订单", "空表", "客户"])
        self.assertTrue(previews[1].empty)
        self.assertEqual(previews[0].types["编码"], "text")

    def test_preview_infers_number_date_and_time_as_text(self):
        preview = parse_workbook(self.make("订单.xlsx", make_workbook))[0]
        self.assertEqual(preview.columns, ["编码", "金额", "下单日期", "备注", "创建时间"])
        self.assertEqual(preview.types["金额"], "number")
        self.assertEqual(preview.types["下单日期"], "date")
        self.assertEqual(preview.types["创建时间"], "text")

    def test_preview_flags_suspicious_and_keeps_five_row_sample(self):
        preview = parse_workbook(self.make("订单.xlsx", make_workbook))[0]
        self.assertEqual(preview.suspicious, ["备注"])
        self.assertEqual(preview.row_count, 2)
        self.assertEqual(preview.sample[0][0], "007")
        self.assertEqual(len(preview.sample), 2)

    def test_preview_marks_sampling_beyond_5000_rows(self):
        path = self.make("大表.xlsx", lambda target: make_large_workbook(target, 5200))
        preview = parse_workbook(path)[0]
        self.assertEqual(preview.row_count, 5200)
        self.assertTrue(preview.sampled)

    def test_empty_preview_has_no_columns(self):
        preview = parse_workbook(self.make("订单.xlsx", make_workbook))[1]
        self.assertEqual(preview.columns, [])
        self.assertEqual(preview.row_count, 0)
        self.assertEqual(preview.types, {})


class TestImportSheets(ImportTestCase):
    def test_import_creates_one_table_per_sheet(self):
        created = import_sheets(
            self.conn,
            self.make("订单.xlsx", make_workbook),
            ["订单", "客户"],
            {},
            "skip",
        )
        self.assertEqual(sorted(created), ["客户", "订单"])
        self.assertEqual(list_tables(self.conn), ["订单", "客户"])

    def test_import_preserves_values_and_types(self):
        import_sheets(
            self.conn,
            self.make("订单.xlsx", make_workbook),
            ["订单"],
            {},
            "skip",
        )
        row = self.rows("订单")[0]
        self.assertEqual(row["编码"], "007")
        self.assertEqual(row["金额"], 10.5)
        self.assertEqual(row["下单日期"], "2024-01-01")
        self.assertEqual(row["创建时间"], "2024-01-01 09:30:00")
        fields = {field.name: field.type for field in get_fields(self.conn, "订单")}
        self.assertEqual(fields["下单日期"], "date")
        self.assertEqual(fields["金额"], "number")

    def test_overwrite_moves_old_table_to_trash(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        self.conn.execute('INSERT INTO "订单" ("created_at", "updated_at", "金额") VALUES ("t", "t", 1)')
        created = import_sheets(
            self.conn,
            self.make("订单.xlsx", make_workbook),
            ["订单"],
            {},
            "overwrite",
        )
        self.assertEqual(created, ["订单"])
        self.assertTrue(list_trashed_tables(self.conn))
        self.assertEqual(self.rows("订单")[0]["编码"], "007")
        old = list_trashed_tables(self.conn)[0]
        self.assertEqual(old["name"], "订单")
        count = self.conn.execute(
            f'SELECT COUNT(*) FROM "{old["table_name"]}"'
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_copy_creates_suffixed_table(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        created = import_sheets(
            self.conn,
            self.make("订单.xlsx", make_workbook),
            ["订单"],
            {},
            "copy",
        )
        self.assertEqual(created, ["订单_2"])
        self.assertEqual(list_tables(self.conn), ["订单", "订单_2"])
        self.assertEqual([f.name for f in get_fields(self.conn, "订单")], ["金额"])
        self.assertIn("编码", [f.name for f in get_fields(self.conn, "订单_2")])

    def test_skip_keeps_existing_table(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        created = import_sheets(
            self.conn,
            self.make("订单.xlsx", make_workbook),
            ["订单"],
            {},
            "skip",
        )
        self.assertEqual(created, [])
        self.assertEqual([f.name for f in get_fields(self.conn, "订单")], ["金额"])
        self.assertEqual(audit.history(self.conn), [])

    def test_duplicate_columns_are_disambiguated(self):
        import_sheets(
            self.conn,
            self.make("重复.xlsx", make_dup_workbook),
            ["重复"],
            {},
            "skip",
        )
        self.assertEqual(
            [f.name for f in get_fields(self.conn, "重复")][4:6], ["金额", "金额_2"]
        )

    def test_failure_rolls_back_everything(self):
        with self.assertRaises(Exception):
            import_sheets(
                self.conn,
                self.make("坏.xlsx", make_bad_workbook),
                ["订单", "客户"],
                {},
                "skip",
            )
        self.assertEqual(list_tables(self.conn), [])
        self.assertEqual(audit.history(self.conn), [])

    def test_types_override_wins_over_inference(self):
        import_sheets(
            self.conn,
            self.make("订单.xlsx", make_workbook),
            ["订单"],
            {"订单": {"金额": "text"}},
            "skip",
        )
        fields = {field.name: field.type for field in get_fields(self.conn, "订单")}
        self.assertEqual(fields["金额"], "text")
        self.assertEqual(self.rows("订单")[0]["金额"], "10.5")

    def test_unknown_type_in_override_is_rejected(self):
        with self.assertRaises(ValueError):
            import_sheets(
                self.conn,
                self.make("订单.xlsx", make_workbook),
                ["订单"],
                {"订单": {"金额": "currency"}},
                "skip",
            )

    def test_empty_sheet_is_skipped(self):
        created = import_sheets(
            self.conn,
            self.make("订单.xlsx", make_workbook),
            ["空表"],
            {},
            "skip",
        )
        self.assertEqual(created, [])
        self.assertEqual(list_tables(self.conn), [])

    def test_large_sheet_is_inserted_in_batches(self):
        import_sheets(
            self.conn,
            self.make("大表.xlsx", make_large_workbook),
            ["大表"],
            {},
            "skip",
        )
        count = self.conn.execute('SELECT COUNT(*) FROM "大表"').fetchone()[0]
        self.assertEqual(count, 600)
        self.assertEqual(self.rows("大表")[-1]["名称"], "名称600")

    def test_reserved_sheet_name_does_not_reach_internal_namespace(self):
        created = import_sheets(
            self.conn,
            self.make("保留.xlsx", make_reserved_workbook),
            ["__lsm_秘密"],
            {},
            "skip",
        )
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].lower().startswith(("__lsm_", "sqlite_")))
        self.assertTrue(self.rows(created[0]))

    def test_xls_is_rejected_with_hint(self):
        target = os.path.join(self.tmp.name, "旧表.xls")
        with open(target, "wb") as handle:
            handle.write(b"not a real file")
        with self.assertRaisesRegex(ValueError, "xlsx"):
            import_sheets(self.conn, target, ["Sheet1"], {}, "skip")

    def test_empty_selection_returns_nothing(self):
        self.assertEqual(
            import_sheets(self.conn, self.make("订单.xlsx", make_workbook), [], {}, "skip"),
            [],
        )
        self.assertEqual(list_tables(self.conn), [])

    def test_missing_file_is_reported_in_chinese(self):
        with self.assertRaisesRegex(ValueError, "无法读取文件"):
            import_sheets(
                self.conn,
                os.path.join(self.tmp.name, "不存在.xlsx"),
                ["订单"],
                {},
                "skip",
            )

    def test_large_sheet_beyond_inference_sample_still_imports(self):
        path = self.make("大表.xlsx", lambda target: make_large_workbook(target, 5200))
        created = import_sheets(self.conn, path, ["大表"], {}, "skip")
        self.assertEqual(created, ["大表"])
        count = self.conn.execute('SELECT COUNT(*) FROM "大表"').fetchone()[0]
        self.assertEqual(count, 5200)
        self.assertEqual(
            self.conn.execute('SELECT "名称" FROM "大表" ORDER BY "id" DESC LIMIT 1').fetchone()[0],
            "名称5200",
        )

    def test_invalid_conflict_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            import_sheets(
                self.conn,
                self.make("订单.xlsx", make_workbook),
                ["订单"],
                {},
                "append",
            )

    def test_audit_records_file_and_each_sheet(self):
        import_sheets(
            self.conn,
            self.make("订单.xlsx", make_workbook),
            ["订单", "客户"],
            {},
            "skip",
        )
        op = audit.history(self.conn, limit=1)[0]
        self.assertEqual(op["kind"], "import")
        self.assertEqual(op["payload"]["file"], "订单.xlsx")
        self.assertEqual(op["payload"]["tables"], ["订单", "客户"])
        sheets = {item["sheet"]: item for item in op["payload"]["sheets"]}
        self.assertEqual(set(sheets), {"订单", "客户"})
        self.assertEqual(sheets["订单"]["rows"], 2)
        self.assertEqual(sheets["客户"]["rows"], 2)
        self.assertEqual(sheets["订单"]["table"], "订单")
        self.assertIsNone(sheets["订单"]["overwritten"])

    def test_audit_marks_overwritten_table(self):
        create_table(self.conn, "订单", [ColumnSpec("金额", "number")])
        import_sheets(
            self.conn,
            self.make("订单.xlsx", make_workbook),
            ["订单"],
            {},
            "overwrite",
        )
        op = audit.history(self.conn, limit=1)[0]
        detail = op["payload"]["sheets"][0]
        self.assertEqual(detail["overwritten"], "订单")
        self.assertIsInstance(detail["overwritten_op"], int)
        trashed = list_trashed_tables(self.conn)[0]
        self.assertEqual(detail["overwritten_trashed_as"], trashed["table_name"])
        self.assertEqual(trashed["trashed_op"], detail["overwritten_op"])


class TestHeaderNames(unittest.TestCase):
    def test_duplicates_are_suffixed(self):
        self.assertEqual(
            _header_names(["金额", "金额", "金额_2"]), ["金额", "金额_2", "金额_2_2"]
        )

    def test_duplicates_ignore_case(self):
        self.assertEqual(_header_names(["Name", "name"]), ["Name", "name_2"])

    def test_blank_header_gets_positional_name(self):
        self.assertEqual(_header_names(["名称", None, "备注"]), ["名称", "Column2", "备注"])

    def test_trailing_blank_columns_are_dropped(self):
        self.assertEqual(_header_names(["名称", None, None]), ["名称"])


class TestSanitize(unittest.TestCase):
    def test_replaces_forbidden_characters(self):
        self.assertEqual(_sanitize_table_name("a/b:c*d?e[f]g\\h"), "a_b_c_d_e_f_g_h")

    def test_falls_back_for_blank_name(self):
        self.assertEqual(_sanitize_table_name("   "), "Sheet1")

    def test_reserved_prefix_is_escaped(self):
        name = _sanitize_table_name("__lsm_x")
        self.assertFalse(name.lower().startswith("__lsm_"))
        self.assertEqual(_sanitize_table_name("sqlite_x"), "_sqlite_x")

class TestCsv(ImportTestCase):
    def test_csv_preview_uses_file_stem_and_infers_types(self):
        previews = parse_workbook(self.make("客户.csv", make_csv))
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0].name, "客户")
        self.assertEqual(previews[0].columns, ["名称", "金额", "编码"])
        self.assertEqual(previews[0].types["编码"], "text")
        self.assertEqual(previews[0].types["金额"], "number")
        self.assertEqual(previews[0].row_count, 2)
        self.assertFalse(previews[0].sampled)

    def test_csv_import_creates_single_table(self):
        created = import_sheets(
            self.conn, self.make("客户.csv", make_csv), ["客户"], {}, "skip"
        )
        self.assertEqual(created, ["客户"])
        row = self.rows("客户")[0]
        self.assertEqual(row["编码"], "007")
        self.assertEqual(row["金额"], 12.5)
        self.assertEqual(list_tables(self.conn), ["客户"])

    def test_csv_with_bom_and_duplicate_columns(self):
        path = self.make("重复.csv", make_csv_bom_dup)
        preview = parse_workbook(path)[0]
        self.assertEqual(preview.columns, ["金额", "金额_2", "名称"])
        created = import_sheets(self.conn, path, ["重复"], {}, "skip")
        self.assertEqual(created, ["重复"])
        self.assertEqual(
            [field.name for field in get_fields(self.conn, "重复")],
            ["金额", "金额_2", "名称"],
        )
        self.assertEqual(self.rows("重复")[0]["名称"], "测试")
