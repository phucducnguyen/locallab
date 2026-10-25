"""Lane A: the bench (bench.run.Bench) writing SQLite per docs/schema.sql.

No board, no network: synthetic timestamped lines in, rows out. Shas are
invented except where a recording's own BOOT line is read at test time.
"""

import json
import os
import sys
import tempfile
import time
import unittest

from bench import parser, store
from bench.run import Bench, ExplainWorker

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(os.path.dirname(os.path.dirname(HERE)), "fw-public", "logs")
SHA = "abcdef1"
TESTS = ("crc32", "ringbuf", "systick", "cmd_dispatch", "loopback")


class Rig:
    def __init__(self, tmp, explainer=None, send_ok=True):
        self.db = os.path.join(tmp, "bench.db")
        self.conn = store.open_db(self.db)
        self.sent = []
        self.logs = []
        self.send_ok = send_ok
        self.bench = Bench(self.conn, "nucleo-1", send=self._send, explainer=explainer,
                           log=self.logs.append)
        self.bench.apply(self.bench.m.link_up(0.0), 0.0)
        self.n = 0

    def _send(self, cmd):
        self.sent.append(cmd)
        return self.send_ok

    def line(self, t, text):
        recs = parser.parse_line(text)["records"]
        self.bench.line(t, "T%.2f" % t, recs)
        self.bench.tick(t + 0.001)

    def health(self, t, n, state):
        self.line(t, "LW ALIVE %d" % n)
        self.line(t + 0.01, "LW HEALTH %d loopback=%s" % (n, state))

    def good_run(self, t, sha=SHA, reset="PIN"):
        self.line(t, "LW BOOT fw=%s reset=%s csr=0x04000000" % (sha, reset))
        for i, name in enumerate(TESTS):
            self.line(t + 0.01 * (i + 1), "LW TEST %s PASS" % name)
        self.line(t + 0.1, "LW RESULT PASS")

    def rows(self, sql, *args):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]


class FakeExplainer:
    def __init__(self):
        self.queued = []

    def queue(self, iid):
        self.queued.append(iid)


class TestJumper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ex = FakeExplainer()
        self.r = Rig(self.tmp.name, explainer=self.ex)

    def tearDown(self):
        self.r.conn.close()
        self.tmp.cleanup()

    def test_verified_pass_then_jumper_pulled_and_back(self):
        r = self.r
        r.bench.expect_flash(SHA, window=60, t=0.5)
        r.line(0.6, "LW HEALTH 99 loopback=OPEN")       # previous build: discarded
        r.good_run(1)
        runs = r.rows("SELECT * FROM runs")
        self.assertEqual([(x["commit"], x["verdict"], x["triage"]) for x in runs],
                         [(SHA, "PASS", "NONE")])
        self.assertEqual(len(r.rows("SELECT * FROM pass_history")), 1)
        self.assertEqual(r.rows("SELECT * FROM routing"), [])  # NONE: no routing row
        self.assertEqual(r.rows("SELECT * FROM flashes")[0]["result"], "VERIFIED")
        self.assertFalse(any("99" in e["fields"] for e in r.rows("SELECT * FROM events")))

        r.health(2, 1, "OK")
        self.assertEqual(r.sent, [])
        r.health(3, 2, "OPEN")
        r.health(4, 3, "OPEN")
        inc = r.rows("SELECT * FROM incidents")
        self.assertEqual(len(inc), 1)
        inc = inc[0]
        self.assertEqual((inc["kind"], inc["state"], inc["triage"], inc["route"]),
                         ("HEALTH_FAIL", "OPEN", "HARDWARE", "Hardware team"))
        self.assertEqual(inc["firmware"], SHA)
        self.assertEqual(inc["last_pass_sha"], SHA)
        self.assertAlmostEqual(inc["t_first_bad_line"], 3.01)
        self.assertAlmostEqual(inc["t_open"], 4.01)
        self.assertIsNone(inc["t_alert_sent"])
        routing = r.rows("SELECT * FROM routing")
        self.assertEqual([(x["incident_id"], x["triage"], x["route"]) for x in routing],
                         [(inc["id"], "HARDWARE", "Hardware team")])
        alerts = r.rows("SELECT * FROM alerts")
        self.assertEqual([a["transition"] for a in alerts], ["open"])
        text = alerts[0]["text"].splitlines()
        self.assertEqual(text[0], "🔴 Problem on test board 1: the test wire (A0–A1) is "
                                  "disconnected.")
        self.assertEqual(text[1], "Who: Hardware team · Not a software bug.")
        self.assertEqual(text[2], "Details: loopback OPEN (A0–A1 wire) · nucleo-1 · fw %s · "
                                  "HARDWARE · #%d" % (inc["firmware"], inc["id"]))
        self.assertEqual(text[3], "Line: LW HEALTH 2 loopback=OPEN  [T3.01]")
        self.assertEqual(text[4], "Alert 1.0 s after the fault.")
        self.assertEqual(len(text), 5)
        self.assertIsNone(alerts[0]["sent"])
        ev = json.loads(inc["evidence"])
        cited = ev["lines"][0]
        row = r.rows("SELECT * FROM events WHERE id=?", cited["event_id"])[0]
        self.assertEqual(json.loads(row["fields"])["line"], cited["line"])
        self.assertEqual(row["raw_ts"], cited["raw_ts"])

        # the diagnostic went out after the alert; its reply becomes a follow-up row
        self.assertEqual(r.sent, ["test loopback"])
        self.assertEqual(self.ex.queued, [])
        r.line(4.2, "LW TEST loopback FAIL pa1=high")
        r.line(4.3, "LW REPLY test done")
        diag = r.rows("SELECT * FROM alerts WHERE transition='diag'")
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0]["reply_to_alert_id"], alerts[0]["id"])
        self.assertIn("LW TEST loopback FAIL pa1=high", diag[0]["text"])
        self.assertIn("[T4.20]", diag[0]["text"])
        self.assertEqual(self.ex.queued, [inc["id"]])
        ev = json.loads(r.rows("SELECT evidence FROM incidents")[0]["evidence"])
        self.assertEqual(len(ev["diagnostic"]["reply"]), 2)

        # still out: no second incident or alert; back in: resolved with a duration
        for i in range(4, 10):
            r.health(i + 1, i, "OPEN")
        for i in range(10, 13):
            r.health(i + 1, i, "OK")
        inc = r.rows("SELECT * FROM incidents")
        self.assertEqual(len(inc), 1)
        self.assertEqual(inc[0]["state"], "RESOLVED")
        self.assertAlmostEqual(inc[0]["duration_s"], 13.01 - 4.01, places=3)
        self.assertEqual(len(r.rows("SELECT * FROM alerts WHERE transition='open'")), 1)
        r.bench.tick(80)
        res = r.rows("SELECT * FROM alerts WHERE transition='resolve'")
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["text"].startswith("✅ Fixed: test board 1's test wire (A0–A1) "
                                                  "is back to normal (after 9 s)."))
        self.assertIn("#%d" % inc[0]["id"], res[0]["text"])
        self.assertEqual(res[0]["reply_to_alert_id"], alerts[0]["id"])
        kinds = [e["kind"] for e in r.rows("SELECT kind FROM events")]
        for k in ("RUN_START", "RUN_END", "INCIDENT_OPEN", "INCIDENT_RESOLVE"):
            self.assertIn(k, kinds)

    def test_without_expect_flash_triage_is_unknown(self):
        r = self.r
        r.good_run(1)
        r.health(2, 1, "OPEN")
        r.health(3, 2, "OPEN")
        inc = r.rows("SELECT * FROM incidents WHERE kind='HEALTH_FAIL'")[0]
        self.assertEqual((inc["triage"], inc["route"]), ("UNKNOWN", "Triage"))
        self.assertIsNone(inc["firmware"])
        self.assertEqual(r.rows("SELECT * FROM runs"), [])
        self.assertIn("Who: Triage", r.rows("SELECT text FROM alerts")[0]["text"])

    def test_diag_skipped_before_result_and_alive(self):
        r = self.r
        r.health(1, 1, "OPEN")
        r.health(2, 2, "OPEN")
        self.assertEqual(r.sent, [])
        self.assertEqual(len(self.ex.queued), 1)
        ev = json.loads(r.rows("SELECT evidence FROM incidents")[0]["evidence"])
        self.assertFalse(ev["diagnostic"]["sent"])

    def test_diag_no_reply_times_out(self):
        r = self.r
        r.good_run(1)
        r.health(2, 1, "OPEN")
        r.health(3, 2, "OPEN")
        self.assertEqual(r.sent, ["test loopback"])
        r.bench.tick(6.5)
        diag = r.rows("SELECT * FROM alerts WHERE transition='diag'")
        self.assertIn("no reply to 'test loopback' within 3 s", diag[0]["text"])

    def test_flaps_write_one_incident_and_rate_limited_alerts(self):
        r = self.r
        t, n = 1.0, 1
        for _ in range(5):
            for s in ("OPEN", "OPEN", "OK", "OK", "OK"):
                r.health(t, n, s)
                t += 1
                n += 1
        inc = r.rows("SELECT * FROM incidents")
        self.assertEqual(len(inc), 1)
        self.assertEqual(inc[0]["flaps"], 4)
        trans = [a["transition"] for a in r.rows("SELECT transition FROM alerts ORDER BY id")]
        self.assertEqual(trans, ["open", "flap-1"])
        for _ in range(62):              # the board keeps reporting OK
            r.health(t, n, "OK")
            t += 1
            n += 1
        trans = [a["transition"] for a in r.rows("SELECT transition FROM alerts ORDER BY id")]
        self.assertEqual(trans, ["open", "flap-1", "resolve"])
        flap = r.rows("SELECT * FROM alerts WHERE transition='flap-1'")[0]
        self.assertTrue(flap["text"].startswith("🟠 Test board 1's test wire (A0–A1) keeps "
                                                "connecting and disconnecting (1 times)."))
        self.assertIn("#%d · loopback (A0–A1) flapping" % inc[0]["id"], flap["text"])
        self.assertEqual(len(r.rows("SELECT * FROM routing")), 1)

    def test_restart_reloads_open_incident_and_sends_nothing_new(self):
        r = self.r
        r.bench.expect_flash(SHA, t=0.5)
        r.good_run(1)
        r.health(2, 1, "OPEN")
        r.health(3, 2, "OPEN")
        self.assertEqual(len(r.rows("SELECT * FROM alerts WHERE transition='open'")), 1)
        # kill -9 and start again on the same DB, jumper still out
        b2 = Bench(r.conn, "nucleo-1", send=lambda c: True, explainer=None, log=lambda s: None)
        self.assertEqual(b2.m.verified_sha, SHA)
        b2.apply(b2.m.link_up(10), 10)
        for i in range(3, 8):
            for txt in ("LW ALIVE %d" % i, "LW HEALTH %d loopback=OPEN" % i):
                b2.line(10 + i, "x", parser.parse_line(txt)["records"])
                b2.tick(10 + i)
        self.assertEqual(len(r.rows("SELECT * FROM incidents")), 1)
        self.assertEqual(len(r.rows("SELECT * FROM alerts")), 1 + 0)  # diag/explain aside
        for i in range(8, 11):
            b2.line(20 + i, "x", parser.parse_line("LW HEALTH %d loopback=OK" % i)["records"])
        self.assertEqual(r.rows("SELECT state FROM incidents")[0]["state"], "RESOLVED")
        self.assertEqual(len(r.rows("SELECT * FROM pass_history")), 1)


class TestRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Rig(self.tmp.name)

    def tearDown(self):
        self.r.conn.close()
        self.tmp.cleanup()

    def test_failed_run_has_one_routing_row_with_both_ids(self):
        r = self.r
        r.bench.expect_flash("1234567", t=0)
        r.good_run(0.1, sha="1234567")
        r.bench.expect_flash(SHA, t=5)
        r.line(6, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        r.line(6.1, "LW TEST ringbuf FAIL got=0x1 want=0x2")
        r.line(6.2, "LW RESULT FAIL")
        run = r.rows("SELECT * FROM runs WHERE verdict='FAIL'")[0]
        inc = r.rows("SELECT * FROM incidents")[0]
        self.assertEqual((inc["kind"], inc["triage"], inc["route"]),
                         ("RUN_FAIL", "CODE", "Firmware team"))
        self.assertEqual(run["triage"], "CODE")
        routing = r.rows("SELECT * FROM routing")
        self.assertEqual([(x["incident_id"], x["run_id"], x["route"]) for x in routing],
                         [(inc["id"], run["run_id"], "Firmware team")])

    def test_hardfault_run_is_code_with_one_incident(self):
        r = self.r
        r.bench.expect_flash(SHA, t=0)
        r.line(1, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        for i in range(5):
            r.line(2 + i, "LW HARDFAULT pc=0x08000100 lr=0x08000201 cfsr=0x1")
        inc = r.rows("SELECT * FROM incidents")
        self.assertEqual([(x["kind"], x["triage"]) for x in inc], [("HARDFAULT", "CODE")])
        self.assertEqual(r.rows("SELECT verdict FROM runs")[0]["verdict"], "HARDFAULT")
        self.assertEqual(len(r.rows("SELECT * FROM alerts")), 1)
        self.assertEqual(len(r.rows("SELECT * FROM routing")), 1)

    def test_flash_error_keeps_pass_history(self):
        r = self.r
        r.bench.expect_flash(SHA, t=0)
        r.good_run(1)
        r.bench.expect_flash("1234567", window=15, t=10)
        r.bench.tick(26)
        runs = r.rows("SELECT * FROM runs ORDER BY run_id")
        self.assertEqual([x["verdict"] for x in runs], ["PASS", "FLASH_ERROR"])
        self.assertIsNone(runs[1]["commit"])
        self.assertEqual(runs[1]["triage"], "UNKNOWN")
        self.assertEqual(len(r.rows("SELECT * FROM pass_history")), 1)
        routing = r.rows("SELECT * FROM routing")
        self.assertEqual([(x["run_id"], x["incident_id"], x["route"]) for x in routing],
                         [(runs[1]["run_id"], None, "Triage")])
        self.assertIsNone(r.bench.m.verified_sha)

    def test_reset_button_on_passing_sha_is_hardware_reboot(self):
        r = self.r
        r.bench.expect_flash(SHA, t=0)
        r.good_run(1)
        r.health(2, 1, "OK")
        r.good_run(3)                       # RESET button: same sha, reset=PIN
        inc = r.rows("SELECT * FROM incidents")
        self.assertEqual([(x["kind"], x["triage"], x["route"]) for x in inc],
                         [("REBOOT", "HARDWARE", "Hardware team")])
        self.assertEqual(len(r.rows("SELECT * FROM runs")), 1)  # not a bench run


@unittest.skipUnless(os.path.isfile(os.path.join(LOGS, "main.raw")), "no recordings")
class TestReplayGoodRecording(unittest.TestCase):
    def test_good_recording_is_one_passing_run_and_no_incident(self):
        with open(os.path.join(LOGS, "main.raw"), "rb") as f:
            lines = f.read().split(b"\n")
        boots = [r for ln in lines for r in parser.parse_line(ln)["records"]
                 if r["kind"] == "BOOT"]
        sha = boots[0]["fw"]
        with tempfile.TemporaryDirectory() as tmp:
            r = Rig(tmp)
            r.bench.expect_flash(sha, t=0)
            t = 1.0
            for ln in lines:
                recs = parser.parse_line(ln.rstrip(b"\r"))["records"]
                if recs:
                    r.bench.line(t, "T", recs)
                    r.bench.tick(t)
                    t += 0.5
            runs = r.rows("SELECT verdict, triage FROM runs")
            self.assertEqual(runs, [{"verdict": "PASS", "triage": "NONE"}])
            self.assertEqual(r.rows("SELECT * FROM incidents"), [])
            r.conn.close()


SCRIPT = """import sys, time
mode = %r
if mode == "ok":
    print("The loopback reads high.\\nDIAGNOSIS: HARDWARE | VERDICT: HEALTH_FAIL | COMMIT: abcdef1 | CAUSE: jumper")
    print("incident", sys.argv[-1])
elif mode == "slow":
    time.sleep(5)
else:
    print("partial")
    sys.exit(2)
"""


class TestExplainWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Rig(self.tmp.name)
        for i in range(3):
            self.r.health(1 + 100 * i, 1, "OPEN")
            self.r.health(2 + 100 * i, 2, "OPEN")
            self.r.health(3 + 100 * i, 3, "OK")
            self.r.health(4 + 100 * i, 4, "OK")
            self.r.health(5 + 100 * i, 5, "OK")
        # (the 100 s gaps with no line also open SILENT incidents; only HEALTH_FAIL is used)
        self.ids = [x["id"] for x in self.r.rows("SELECT id FROM incidents WHERE "
                                                 "kind='HEALTH_FAIL' ORDER BY id")]
        self.assertEqual(len(self.ids), 3)

    def tearDown(self):
        self.r.conn.close()
        self.tmp.cleanup()

    def worker(self, mode, timeout=10, agent_dir=None):
        path = os.path.join(self.tmp.name, "fake_explain_%s.py" % mode)
        with open(path, "w") as f:
            f.write(SCRIPT % mode)
        return ExplainWorker(self.r.db, cmd=[sys.executable, path], timeout=timeout,
                             agent_dir=agent_dir or self.tmp.name, log=lambda s: None)

    def explain_rows(self):
        return self.r.rows("SELECT * FROM alerts WHERE transition='explain' ORDER BY id")

    def test_success_replies_to_first_alert_newest_first(self):
        w = self.worker("ok")
        for iid in self.ids:
            w.queue(iid)
        done = [w.run_one()[0] for _ in self.ids]
        self.assertEqual(done, sorted(self.ids, reverse=True))
        rows = self.explain_rows()
        self.assertEqual(len(rows), 3)
        for row in rows:
            iid = row["incident_id"]
            opened = self.r.rows("SELECT id FROM alerts WHERE incident_id=? AND "
                                 "transition='open'", iid)[0]["id"]
            self.assertEqual(row["reply_to_alert_id"], opened)
            self.assertTrue(row["text"].startswith("🧠 What the AI found (#%d): The loopback "
                                                   "reads high." % iid))
            self.assertTrue(row["text"].splitlines()[-1].startswith("DIAGNOSIS: HARDWARE"))
            ev = json.loads(self.r.rows("SELECT evidence FROM incidents WHERE id=?",
                                        iid)[0]["evidence"])
            self.assertIn("incident %d" % iid, ev["explanation"])   # the full answer
        self.assertIsNone(w.run_one())

    def test_failure_exit_2(self):
        w = self.worker("fail")
        w.queue(self.ids[0])
        self.assertEqual(w.run_one(), (self.ids[0], False))
        self.assertIn("explanation unavailable; triage stands", self.explain_rows()[0]["text"])
        self.assertNotIn("partial", self.explain_rows()[0]["text"])

    def test_timeout(self):
        w = self.worker("slow", timeout=0.5)
        w.queue(self.ids[0])
        t0 = time.monotonic()
        self.assertEqual(w.run_one(), (self.ids[0], False))
        self.assertLess(time.monotonic() - t0, 4)
        self.assertIn("explanation unavailable", self.explain_rows()[0]["text"])

    def test_agent_dir_missing(self):
        w = self.worker("ok", agent_dir=os.path.join(self.tmp.name, "no-agent"))
        w.queue(self.ids[1])
        self.assertEqual(w.run_one(), (self.ids[1], False))
        self.assertIn("explanation unavailable", self.explain_rows()[0]["text"])

    def test_one_explain_row_per_incident(self):
        w = self.worker("ok")
        w.queue(self.ids[0])
        w.run_one()
        w.queue(self.ids[0])
        w.run_one()
        self.assertEqual(len(self.explain_rows()), 1)

    def test_flaps_queue_no_explanation(self):
        ex = FakeExplainer()
        with tempfile.TemporaryDirectory() as tmp:
            r = Rig(tmp, explainer=ex)
            t, n = 1.0, 1
            for _ in range(3):
                for s in ("OPEN", "OPEN", "OK", "OK", "OK"):
                    r.health(t, n, s)
                    t += 1
                    n += 1
            self.assertEqual(len(ex.queued), 1)
            r.conn.close()


if __name__ == "__main__":
    unittest.main()
