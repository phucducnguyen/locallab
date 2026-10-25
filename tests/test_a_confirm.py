"""Lane A: confirm before blaming (bench.confirm) with a fake board.

The fake flash_and_capture stands in for stage A3's hook: it records the
sha it was asked to flash and returns a canned Capture. Shas are invented.
"""

import json
import os
import shutil
import tempfile
import unittest

from bench import alerts, confirm, store

NEW, LAST = "1234abc", "fedc987"


class FakeBoard:
    """flash_and_capture(sha) for a board that is broken or healthy."""

    def __init__(self, healthy):
        self.healthy = healthy
        self.flashed = []

    def __call__(self, sha):
        self.flashed.append(sha)
        if self.healthy:
            lines = ["LW BOOT fw=%s reset=PIN csr=0x0c000000" % sha, "LW TEST loopback PASS",
                     "LW RESULT PASS"]
            return confirm.Capture("PASS", [{"event_id": 1, "raw_ts": "x", "line": ln}
                                            for ln in lines], run_id=None)
        return confirm.Capture("NO_OUTPUT", [])


class LoopbackDeadBoard(FakeBoard):
    def __call__(self, sha):
        self.flashed.append(sha)
        return confirm.Capture("FAIL", ["LW BOOT fw=%s reset=PIN csr=0x0c000000" % sha,
                                        "LW TEST loopback FAIL open", "LW RESULT FAIL"])


class Rig:
    def __init__(self, tmp, verdict="NO_OUTPUT", kind="RUN_FAIL"):
        self.conn = store.open_db(os.path.join(tmp.tmp, "bench.db"))
        tmp.rigs.append(self)
        c = self.conn
        with c:
            self.run_id = store.insert(c, "runs", {
                "commit": NEW, "verdict": verdict, "triage": "UNKNOWN",
                "evidence": "{}", "t_start": 100.0, "t_end": 115.0})
            self.iid = store.insert(c, "incidents", {
                "board": "nucleo-1", "kind": kind, "state": "OPEN", "opened": 115.0,
                "firmware": NEW, "last_pass_sha": LAST, "last_pass_t": 50.0,
                "triage": "UNKNOWN", "reason": "Verified sha %s ended its run as %s." % (
                    NEW, verdict),
                "evidence": json.dumps({"run_id": self.run_id, "file_line": None}),
                "route": "Triage", "t_open": 115.0})
            store.insert(c, "routing", {"incident_id": self.iid, "run_id": self.run_id,
                                        "triage": "UNKNOWN", "route": "Triage",
                                        "reason": "x", "ticket": "42", "created": 115.0})
            self.open_aid = store.add_alert(c, self.iid, "open", "first", 115.0)

    def one(self, sql, *a):
        r = self.conn.execute(sql, a).fetchone()
        return dict(r) if r else None

    def incident(self):
        return self.one("SELECT * FROM incidents WHERE id=?", self.iid)


class ConfirmFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rigs = []

    def tearDown(self):
        for r in self.rigs:
            r.conn.close()
        shutil.rmtree(self.tmp)

    def check_written(self, rig, triage, route):
        inc = rig.incident()
        self.assertEqual((inc["triage"], inc["route"]), (triage, route))
        ev = json.loads(inc["evidence"])
        self.assertEqual(ev["confirm"]["to"], triage)
        self.assertEqual(ev["confirm"]["reflashed_sha"], LAST)
        self.assertEqual(ev["run_id"], rig.run_id)  # earlier evidence kept
        run = rig.one("SELECT * FROM runs WHERE run_id=?", rig.run_id)
        self.assertEqual(run["triage"], triage)
        routing = rig.conn.execute("SELECT * FROM routing").fetchall()
        self.assertEqual(len(routing), 1)  # updated in place
        self.assertEqual((routing[0]["triage"], routing[0]["route"], routing[0]["ticket"]),
                         (triage, route, "42"))
        a = rig.one("SELECT * FROM alerts WHERE transition='confirmed'")
        self.assertEqual(a["reply_to_alert_id"], rig.open_aid)
        self.assertIsNone(a["sent"])
        self.assertIn("Team: %s." % (route or "Triage").replace(" team", "").lower(), a["text"])
        self.assertIn("(#%d)" % rig.iid, a["text"])
        self.assertIn("/ask %d" % rig.iid, a["text"])
        self.assertLessEqual(len(a["text"].splitlines()), 6)
        ev = rig.one("SELECT * FROM events WHERE kind='TRIAGE_CONFIRM'")
        self.assertEqual(json.loads(ev["fields"])["to"], triage)
        return a

    def test_last_pass_runs_clean_is_code(self):
        rig, board = Rig(self), FakeBoard(healthy=True)
        res = confirm.confirm(rig.incident(), LAST, board, conn=rig.conn, t=130.0)
        self.assertEqual(board.flashed, [LAST])
        self.assertEqual((res.status, res.triage, res.route), ("CONFIRMED", "CODE",
                                                               "Firmware team"))
        self.assertTrue(res.output_returned)
        self.assertIn(NEW, res.reason)
        self.assertIn("NO_OUTPUT", res.reason)
        self.assertIn("ran clean", res.reason)
        a = self.check_written(rig, "CODE", "Firmware team")
        self.assertIn("It is the code, not the board.", a["text"])

    def test_last_pass_still_fails_is_hardware(self):
        rig, board = Rig(self), FakeBoard(healthy=False)
        res = confirm.confirm(rig.incident(), LAST, board, conn=rig.conn, t=130.0)
        self.assertEqual((res.triage, res.route), ("HARDWARE", "Hardware team"))
        self.assertIs(res.output_returned, False)
        self.assertIn("no LW output returned", res.reason)
        a = self.check_written(rig, "HARDWARE", "Hardware team")
        self.assertIn("It is the board, not the code.", a["text"])

    def test_loopback_only_new_sha_then_reflash_fails_is_hardware(self):
        rig = Rig(self, verdict="FAIL")
        self.assertTrue(confirm.needs_confirm("UNKNOWN", verdict="FAIL",
                                              tests={"crc32": "PASS", "loopback": "FAIL"}))
        res = confirm.confirm({"incident_id": rig.iid, "run_id": rig.run_id, "verdict": "FAIL",
                               "firmware": NEW, "triage": "UNKNOWN"}, LAST,
                              LoopbackDeadBoard(False), conn=rig.conn, t=130.0)
        self.assertEqual(res.triage, "HARDWARE")
        self.assertTrue(res.output_returned)
        self.assertIn("LW BOOT fw=%s" % LAST, res.reason)
        self.check_written(rig, "HARDWARE", "Hardware team")

    def test_once_only(self):
        rig = Rig(self)
        confirm.confirm(rig.incident(), LAST, FakeBoard(True), conn=rig.conn, t=130.0)
        res = confirm.confirm(rig.incident(), LAST, FakeBoard(True), conn=rig.conn, t=131.0)
        self.assertEqual(res.triage, "CODE")
        n = rig.one("SELECT COUNT(*) AS n FROM alerts WHERE transition='confirmed'")["n"]
        self.assertEqual(n, 1)

    def test_not_confirmed_stays_unknown(self):
        rig = Rig(self)
        res = confirm.confirm(rig.incident(), None, FakeBoard(True), conn=rig.conn, t=130.0)
        self.assertEqual((res.status, res.triage, res.route), ("NOT_CONFIRMED", "UNKNOWN",
                                                               "Triage"))
        self.assertIn("no passing sha", res.reason)

        def boom(sha):
            raise OSError("drive gone")
        res = confirm.confirm(rig.incident(), LAST, boom)
        self.assertEqual(res.triage, "UNKNOWN")
        self.assertIn("failed to run", res.reason)
        res = confirm.confirm(rig.incident(), LAST, lambda s: confirm.Capture("BUILD_ERROR"))
        self.assertEqual(res.triage, "UNKNOWN")
        a = rig.one("SELECT text FROM alerts WHERE transition='confirmed'")
        self.assertIn("could not tell if it is the board or the code", a["text"])

    def test_run_without_incident(self):
        rig = Rig(self, verdict="GARBLED")
        run = rig.one("SELECT * FROM runs WHERE run_id=?", rig.run_id)
        res = confirm.confirm(run, LAST, FakeBoard(True), conn=rig.conn, t=130.0)
        self.assertEqual(res.triage, "CODE")
        self.assertIn("GARBLED", res.reason)
        self.assertEqual(rig.one("SELECT triage FROM runs")["triage"], "CODE")
        self.assertEqual(rig.one("SELECT triage FROM routing")["triage"], "CODE")
        self.assertIsNone(rig.one("SELECT * FROM alerts WHERE transition='confirmed'"))

    def test_needs_confirm(self):
        nc = confirm.needs_confirm
        for v in ("NO_OUTPUT", "GARBLED", "RESET_LOOP", "FLASH_ERROR"):
            self.assertTrue(nc("UNKNOWN", verdict=v))
        self.assertTrue(nc("UNKNOWN", kind="SILENT"))
        self.assertTrue(nc("UNKNOWN", kind="HEALTH_FAIL"))
        self.assertFalse(nc("CODE", verdict="NO_OUTPUT"))
        self.assertFalse(nc("UNKNOWN", kind="REBOOT"))
        self.assertFalse(nc("UNKNOWN", verdict="FAIL", tests={"crc32": "FAIL"}))
        self.assertFalse(nc("UNKNOWN", verdict="FAIL",
                            tests={"crc32": "FAIL", "loopback": "FAIL"}))

    def test_open_alert_text_unchanged_without_fault(self):
        rig = Rig(self)
        text = alerts.alert_text("open", rig.incident())
        self.assertNotIn("Fault:", text)


if __name__ == "__main__":
    unittest.main()
