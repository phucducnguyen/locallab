"""Lane V: the visual dashboard (bench/dashboard.py): /state.json, the page, read-only, WAL."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from bench import dashboard
from stub import make_db
from stub.db import Writer, connect, init


def add_health(conn, t, line=None, board="nucleo-1", **fields):
    if line is not None:
        fields["line"] = line
    conn.execute("INSERT INTO events (t_wall, board, kind, fields, raw_ts) VALUES (?,?,?,?,?)",
                 (t, board, "HEALTH", json.dumps(fields), "10:00:00.000"))
    conn.commit()


def ok_fields(**over):
    f = {"n": 7, "loopback": "OK", "sensor": "OK", "radio": "OK", "estop": "OFF",
         "vdd": 3301, "temp": 31.5}
    f.update(over)
    line = "LW HEALTH 7 " + " ".join("%s=%s" % (k, f[k]) for k in
                                     ("loopback", "sensor", "radio", "estop", "vdd", "temp"))
    return f, line


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "bench.db")

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self):
        make_db.build(self.path)
        return connect(self.path)

    def empty(self):
        conn = connect(self.path)
        init(conn)
        return conn

    def state(self):
        d = dashboard.Dashboard(self.path)
        try:
            return d.state()
        finally:
            d.db.close()

    def devs(self, st):
        return {k: v["state"] for k, v in st["devices"].items()}


class StateTest(Base):
    def test_all_ok(self):
        conn = self.empty()
        f, line = ok_fields()
        add_health(conn, time.time(), line, **f)
        conn.close()
        st = self.state()
        self.assertIsNone(st["error"])
        self.assertEqual(self.devs(st), {"loopback": "OK", "sensor": "OK", "radio": "OK",
                                         "estop": "OFF", "power": "OK"})
        self.assertEqual(st["banner"]["level"], "ok")
        self.assertEqual(st["banner"]["text"], "ALL CLEAR")
        self.assertEqual(st["vdd"], 3.301)
        self.assertFalse(st["vdd_low"])
        self.assertEqual(st["temp"], 31.5)
        self.assertIn("No cloud AI calls", st["footer"])
        self.assertNotIn("0 bytes", st["footer"])

    def test_faults_from_parsed_keys(self):
        conn = self.stub()
        for over, want in (({"sensor": "OPEN"}, ("sensor", "OPEN")),
                           ({"radio": "STUCK"}, ("radio", "STUCK")),
                           ({"estop": "ON"}, ("estop", "ON")),
                           ({"vdd": 2950}, ("power", "LOW"))):
            f, line = ok_fields(**over)
            add_health(conn, time.time(), line, **f)
            st = self.state()
            d = self.devs(st)
            self.assertEqual(d[want[0]], want[1], over)
            for k in ("loopback", "sensor", "radio"):
                if k != want[0]:
                    self.assertEqual(d[k], "OK", (over, k))
        self.assertEqual(st["vdd"], 2.95)
        self.assertTrue(st["vdd_low"])
        conn.close()

    def test_fallback_to_line_and_absent_fields(self):
        conn = self.empty()
        # an older row: only the line, no parsed keys for the new devices
        add_health(conn, time.time(), "LW HEALTH 9 loopback=OK sensor=OPEN radio=STUCK "
                   "estop=ON vdd=2950 temp=40.0", n=9, loopback="OK")
        st = self.state()
        self.assertEqual(self.devs(st), {"loopback": "OK", "sensor": "OPEN", "radio": "STUCK",
                                         "estop": "ON", "power": "LOW"})
        self.assertEqual(st["temp"], 40.0)
        self.assertEqual(st["banner"]["level"], "warn")  # faults shown, no incident yet
        # the oldest form: loopback only, everything else unknown (grey)
        add_health(conn, time.time(), "LW HEALTH 10 loopback=OK", n=10, loopback="OK")
        conn.close()
        st = self.state()
        self.assertEqual(self.devs(st), {"loopback": "OK", "sensor": "UNKNOWN",
                                         "radio": "UNKNOWN", "estop": "UNKNOWN",
                                         "power": "UNKNOWN"})
        self.assertIsNone(st["vdd"])
        self.assertEqual(st["banner"]["text"], "ALL CLEAR")

    def test_stub_db_banner_and_strip(self):
        self.stub().close()
        st = self.state()
        self.assertEqual(st["banner"]["level"], "bad")
        self.assertEqual(st["banner"]["text"], "CODE: HardFault at src/demo.c:42")
        self.assertTrue(st["banner"]["causes"])
        self.assertEqual(st["devices"]["loopback"]["state"], "OPEN")
        inc = st["devices"]["loopback"]["incident"]
        self.assertEqual((inc["triage"], inc["kind"]), ("HARDWARE", "HEALTH_FAIL"))
        self.assertIsNotNone(inc["alert_s"])
        self.assertIn("LOCAL LAB", st["telegram"]["text"])
        sw = st["stopwatch"]
        self.assertEqual(sw["incident"], st["banner"]["incident"])
        self.assertIsNotNone(sw["alert_s"])

    def test_device_incident_banner(self):
        conn = self.stub()
        f, line = ok_fields(sensor="OPEN")
        t = time.time()
        add_health(conn, t, line, **f)
        w = Writer(conn)
        iid = w.open_incident(t, "HEALTH_FAIL", "HARDWARE", "sensor link open",
                              {"device": "sensor", "fields": {"sensor": "OPEN"}},
                              t_first_bad=t - 1.0)
        conn.execute("UPDATE incidents SET t_alert_sent=?, ticket=? WHERE id=?",
                     (t + 1.5, "12", iid))
        conn.commit()
        conn.close()
        st = self.state()
        self.assertEqual(st["banner"]["text"], "HARDWARE: sensor link OPEN (A2-A3)")
        self.assertEqual(st["banner"]["incident"], iid)
        inc = st["devices"]["sensor"]["incident"]
        self.assertEqual((inc["id"], inc["alert_s"], inc["ticket"]), (iid, 2.5, "12"))

    def test_missing_db(self):
        st = self.state()
        self.assertTrue(st["error"])
        self.assertEqual(st["banner"]["level"], "unknown")


class ServerTest(Base):
    def serve(self):
        srv = dashboard.make_server(self.path, "127.0.0.1", 0)
        th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
        th.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close(), srv.dash.db.close()))
        return "http://127.0.0.1:%d" % srv.server_address[1], srv

    def digest(self):
        with open(self.path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def get(self, url):
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def test_page_is_self_contained(self):
        self.stub().close()
        base, _ = self.serve()
        code, ctype, body = self.get(base + "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        html = body.decode("utf-8")
        self.assertEqual(re.findall(r"(?i)\b(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", html), [])
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotRegex(html, r"(?i)<(script|link|img|iframe)[^>]+(src|href)=")
        self.assertNotIn("@import", html)
        for word in ("<svg", "CN8", "A0", "A5", "B1", "estop", "state.json",
                     "Qwen3.6-35B-A3B", "test wire", "sensor wire", "radio wire",
                     "stop button", "Waiting for the board", "fetch("):
            self.assertIn(word, html)
        code, ctype, body = self.get(base + "/state.json")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["banner"]["level"], "bad")

    def test_never_writes_db(self):
        self.stub().close()
        before = self.digest()
        base, srv = self.serve()
        for _ in range(5):
            self.get(base + "/state.json")
        with self.assertRaises(sqlite3.OperationalError):
            srv.dash.db.conn.execute("INSERT INTO events (t_wall, board, kind, raw_ts) "
                                     "VALUES (1, 'x', 'HEALTH', 'x')")
        self.assertEqual(self.digest(), before)

    def test_answers_while_another_thread_writes(self):
        conn = self.stub()
        conn.close()
        base, _ = self.serve()
        stop = threading.Event()
        errors = []

        def writer():
            c = connect(self.path)
            n = 0
            try:
                while not stop.is_set():
                    n += 1
                    f, line = ok_fields(n=n, sensor="OPEN" if n % 2 else "OK")
                    add_health(c, time.time(), line, **f)
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)
            finally:
                c.close()

        th = threading.Thread(target=writer)
        th.start()
        try:
            seen = set()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                code, _, body = self.get(base + "/state.json")
                self.assertEqual(code, 200)
                st = json.loads(body)
                self.assertIsNone(st["error"])
                seen.add(st["devices"]["sensor"]["state"])
        finally:
            stop.set()
            th.join()
        self.assertEqual(errors, [])
        self.assertTrue(seen & {"OPEN", "OK"})

    def test_banner_turns_red_within_1s(self):
        conn = self.empty()
        f, line = ok_fields()
        add_health(conn, time.time(), line, **f)
        base, _ = self.serve()
        self.assertEqual(json.loads(self.get(base + "/state.json")[2])["banner"]["level"], "ok")
        t = time.time()
        Writer(conn).open_incident(t, "HEALTH_FAIL", "HARDWARE", "radio stuck",
                                   {"device": "radio", "fields": {"radio": "STUCK"}})
        conn.close()
        st = None
        while time.time() - t < 1.0:
            st = json.loads(self.get(base + "/state.json")[2])
            if st["banner"]["level"] == "bad":
                break
            time.sleep(0.05)
        self.assertEqual(st["banner"]["level"], "bad")
        self.assertEqual(st["banner"]["text"], "HARDWARE: radio link STUCK (A4-A5)")

    def test_unknown_path(self):
        self.empty().close()
        base, _ = self.serve()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(base + "/../etc/passwd")
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()


if __name__ == "__main__":
    unittest.main()
