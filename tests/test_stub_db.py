import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from stub import make_db
from stub.db import connect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = {
    "incidents": {"id", "board", "kind", "state", "opened", "closed", "duration_s", "flaps",
                  "firmware", "last_pass_sha", "last_pass_t", "triage", "reason", "evidence",
                  "route", "ticket", "t_first_bad_line", "t_open", "t_alert_sent"},
    "runs": {"run_id", "commit", "subject", "verdict", "triage", "evidence", "duration_s",
             "t_start", "t_end"},
    "events": {"id", "t_wall", "board", "kind", "fields", "raw_ts"},
    "routing": {"id", "incident_id", "run_id", "triage", "route", "reason", "ticket", "created"},
    "alerts": {"id", "incident_id", "transition", "text", "reply_to_alert_id",
               "telegram_message_id", "created", "sent"},
}


class StubDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "bench.db")
        make_db.build(self.path)
        self.c = connect(self.path)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_schema_columns_and_wal(self):
        for table, cols in TABLES.items():
            got = {r["name"] for r in self.c.execute("PRAGMA table_info(%s)" % table)}
            self.assertEqual(got, cols, table)
        self.assertEqual(self.c.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_alert_unique_per_transition(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.c.execute("INSERT INTO alerts (incident_id, transition, text, created) "
                           "VALUES (1, 'open', 'x', 0)")

    def test_canned_records(self):
        inc = {r["id"]: r for r in self.c.execute("SELECT * FROM incidents")}
        jumper, flap, fault = inc[1], inc[2], inc[3]
        self.assertEqual((jumper["kind"], jumper["triage"], jumper["route"], jumper["state"]),
                         ("HEALTH_FAIL", "HARDWARE", "Hardware team", "RESOLVED"))
        self.assertGreater(jumper["duration_s"], 0)
        self.assertEqual((flap["state"], flap["flaps"]), ("FLAPPING", 4))
        self.assertEqual((fault["kind"], fault["triage"], fault["route"], fault["firmware"]),
                         ("HARDFAULT", "CODE", "Firmware team", "2222222"))
        self.assertEqual(json.loads(fault["evidence"])["file_line"], "src/demo.c:42")
        self.assertEqual(fault["last_pass_sha"], "1111111")
        for i in inc.values():
            self.assertLessEqual(i["t_first_bad_line"], i["t_open"])
            self.assertLess(i["t_alert_sent"] - i["t_first_bad_line"], 5.0)
            self.assertLessEqual(len(i["reason"]), 120)
        runs = {r["run_id"]: r for r in self.c.execute("SELECT * FROM runs")}
        self.assertEqual((runs[1]["commit"], runs[1]["verdict"]), ("1111111", "PASS"))
        self.assertEqual((runs[2]["commit"], runs[2]["verdict"], runs[2]["triage"]),
                         ("2222222", "HARDFAULT", "CODE"))

    def test_every_incident_and_failed_run_is_routed(self):
        for r in self.c.execute("SELECT * FROM incidents"):
            rows = self.c.execute("SELECT * FROM routing WHERE incident_id=?", (r["id"],)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["route"], r["route"])
        routed_run = self.c.execute("SELECT run_id FROM routing WHERE run_id=2").fetchone()
        self.assertIsNotNone(routed_run)
        self.assertIsNone(self.c.execute("SELECT 1 FROM routing WHERE run_id=1").fetchone())

    def test_alerts_wording_and_replies(self):
        alerts = self.c.execute("SELECT * FROM alerts ORDER BY id").fetchall()
        self.assertEqual([(a["incident_id"], a["transition"]) for a in alerts],
                         [(1, "open"), (1, "resolve"), (2, "open"), (2, "flap-1"), (3, "open")])
        for a in alerts:
            self.assertIn("Routed to: ", a["text"])
            self.assertIn("Incident: %d" % a["incident_id"], a["text"])
            self.assertIsNotNone(a["sent"])
            if a["transition"] != "open":
                opener = self.c.execute("SELECT id FROM alerts WHERE incident_id=? AND "
                                        "transition='open'", (a["incident_id"],)).fetchone()
                self.assertEqual(a["reply_to_alert_id"], opener["id"])
        fault_open = alerts[4]["text"]
        self.assertIn("File: src/demo.c:42", fault_open)
        self.assertIn("Routed to: Firmware team", fault_open)

    def test_evidence_citations_resolve_to_events(self):
        blobs = [r["evidence"] for r in self.c.execute("SELECT evidence FROM incidents")]
        blobs += [r["evidence"] for r in self.c.execute("SELECT evidence FROM runs")]
        cites = []

        def walk(v):
            if isinstance(v, dict):
                if "event_id" in v:
                    cites.append(v)
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        for b in blobs:
            walk(json.loads(b))
        self.assertGreater(len(cites), 10)
        for c in cites:
            ev = self.c.execute("SELECT * FROM events WHERE id=?", (c["event_id"],)).fetchone()
            self.assertEqual(ev["raw_ts"], c["raw_ts"])
            self.assertEqual(json.loads(ev["fields"])["line"], c["line"])
            self.assertRegex(c["raw_ts"], r"^\d\d:\d\d:\d\d\.\d{3}$")

    def test_cli_refuses_to_overwrite(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "stub", "make_db.py"),
                            "--out", self.path], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        fresh = os.path.join(self.tmp.name, "fresh.db")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "stub", "make_db.py"),
                            "--out", fresh], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.exists(fresh))


if __name__ == "__main__":
    unittest.main()
