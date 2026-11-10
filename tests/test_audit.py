import base64
import datetime
import decimal
import json
import re
import sqlite3
import unittest

from local_sql_manager.services import audit
from local_sql_manager.services.library import open_library


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.lib = open_library(":memory:")
        self.conn = self.lib.conn

    def tearDown(self):
        self.conn.close()

    def test_record_returns_op_with_fields(self):
        op = audit.record(self.conn, "update", "订单", "web", {"rows": []})
        self.assertIsInstance(op, int)
        row = audit.get_op(self.conn, op)
        self.assertEqual(row["id"], op)
        self.assertEqual(row["kind"], "update")
        self.assertEqual(row["table"], "订单")
        self.assertEqual(row["source"], "web")
        self.assertEqual(row["payload"], {"rows": []})
        self.assertEqual(row["irreversible"], 0)
        self.assertIsNone(row["undone_by"])
        self.assertRegex(row["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(
            set(row),
            {
                "id",
                "ts",
                "source",
                "kind",
                "table",
                "payload",
                "undone_by",
                "irreversible",
            },
        )

    def test_payload_stored_as_json_text(self):
        op = audit.record(
            self.conn, "update", "订单", "web", {"rows": [{"id": 1, "after": "苹果"}]}
        )
        raw = self.conn.execute(
            'SELECT "payload_json" FROM "__lsm_op" WHERE "id" = ?', (op,)
        ).fetchone()[0]
        self.assertEqual(
            json.loads(raw), {"rows": [{"id": 1, "after": "苹果"}]}
        )

    def test_get_op_missing_returns_none(self):
        self.assertIsNone(audit.get_op(self.conn, 999))

    def test_history_newest_first(self):
        first = audit.record(self.conn, "insert", "订单", "web", {})
        second = audit.record(self.conn, "update", "订单", "mcp", {})
        third = audit.record(self.conn, "update", "客户", "web", {})
        ids = [row["id"] for row in audit.history(self.conn)]
        self.assertEqual(ids, [third, second, first])

    def test_history_filters_by_table(self):
        audit.record(self.conn, "insert", "订单", "web", {})
        other = audit.record(self.conn, "insert", "客户", "web", {})
        rows = audit.history(self.conn, "客户")
        self.assertEqual([row["id"] for row in rows], [other])

    def test_history_respects_limit(self):
        for _ in range(5):
            audit.record(self.conn, "update", "订单", "web", {})
        self.assertEqual(len(audit.history(self.conn, limit=2)), 2)

    def test_history_parses_payload(self):
        audit.record(self.conn, "insert", "订单", "web", {"rows": [{"id": 7}]})
        self.assertEqual(audit.history(self.conn)[0]["payload"], {"rows": [{"id": 7}]})

    def test_irreversible_flag_survives(self):
        op = audit.record(self.conn, "purge", "订单", "mcp", {}, irreversible=True)
        self.assertEqual(audit.get_op(self.conn, op)["irreversible"], 1)

    def test_mark_undone_sets_undone_by(self):
        op = audit.record(self.conn, "insert", "订单", "web", {})
        undo_id = audit.record(self.conn, "undo", "订单", "web", {"op": op})
        audit.mark_undone(self.conn, op, undo_id)
        self.assertEqual(audit.get_op(self.conn, op)["undone_by"], undo_id)
        self.assertIsNone(audit.get_op(self.conn, undo_id)["undone_by"])


    def test_payload_encodes_bytes_and_dates(self):
        op = audit.record(
            self.conn,
            "import",
            "订单",
            "web",
            {
                "blob": b"\x00\x01",
                "bytearray": bytearray(b"ab"),
                "when": datetime.datetime(2026, 11, 10, 8, 0, 0),
                "day": datetime.date(2026, 11, 10),
                "money": decimal.Decimal("1.50"),
            },
        )
        payload = audit.get_op(self.conn, op)["payload"]
        self.assertEqual(payload["blob"], {"b64": "AAE="})
        self.assertEqual(payload["bytearray"], {"b64": base64.b64encode(b"ab").decode()})
        self.assertEqual(payload["when"], "2026-11-10T08:00:00")
        self.assertEqual(payload["day"], "2026-11-10")
        self.assertEqual(payload["money"], "1.50")

    def test_unencodable_payload_raises(self):
        with self.assertRaises(TypeError):
            audit.record(self.conn, "update", "订单", "web", {"bad": object()})

    def test_none_payload_becomes_empty_dict(self):
        op = audit.record(self.conn, "update", "订单", "web", None)
        self.assertEqual(audit.get_op(self.conn, op)["payload"], {})

    def test_record_does_not_commit(self):
        op = audit.record(self.conn, "update", "订单", "web", {})
        self.conn.rollback()
        self.assertIsNone(audit.get_op(self.conn, op))

    def test_mark_undone_does_not_commit(self):
        op = audit.record(self.conn, "insert", "订单", "web", {})
        undo_id = audit.record(self.conn, "undo", "订单", "web", {"op": op})
        self.conn.commit()
        self.assertEqual(audit.mark_undone(self.conn, op, undo_id), 1)
        self.conn.rollback()
        self.assertIsNone(audit.get_op(self.conn, op)["undone_by"])

    def test_mark_undone_is_idempotent_and_reports_rowcount(self):
        op = audit.record(self.conn, "insert", "订单", "web", {})
        undo_one = audit.record(self.conn, "undo", "订单", "web", {})
        undo_two = audit.record(self.conn, "undo", "订单", "web", {})
        self.assertEqual(audit.mark_undone(self.conn, op, undo_one), 1)
        self.assertEqual(audit.mark_undone(self.conn, op, undo_two), 0)
        self.assertEqual(audit.get_op(self.conn, op)["undone_by"], undo_one)
        self.assertEqual(audit.mark_undone(self.conn, 999, undo_one), 0)

    def test_history_clamps_limit(self):
        for _ in range(3):
            audit.record(self.conn, "update", "订单", "web", {})
        self.assertEqual(audit.history(self.conn, limit=0), [])
        self.assertEqual(audit.history(self.conn, limit=-5), [])
        self.assertEqual(len(audit.history(self.conn, limit=501)), 3)


if __name__ == "__main__":
    unittest.main()