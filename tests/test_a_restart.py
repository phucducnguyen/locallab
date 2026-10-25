"""Lane A stage A5: restart safety. A bench killed with SIGKILL and started again on the
same DB reloads its open incidents and timers and never resends or re-alerts.

No board, no network: a child process plays the bench (bench.run.Bench fed with
synthetic lines), the fake Telegram is on 127.0.0.1.
"""

import io
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from bench import parser, store
from bench.run import Bench
from bench.sender import Sender, Telegram
from tests.test_a_sender import FAKE_CHAT, FAKE_TOKEN, FakeTelegram

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA = "abcdef1"

CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, %(repo)r)
    from bench import parser, store
    from bench.run import Bench
    conn = store.open_db(%(db)r)
    b = Bench(conn, "nucleo-1", log=lambda s: None)
    t = time.time()
    b.apply(b.m.link_up(t), t)
    b.expect_flash(%(sha)r, t=t)
    lines = ["LW BOOT fw=%(sha)s reset=PIN csr=0x04000000"]
    lines += ["LW TEST %%s PASS" %% n for n in ("crc32", "ringbuf", "systick",
                                               "cmd_dispatch", "loopback")]
    lines += ["LW RESULT PASS", "LW ALIVE 1", "LW HEALTH 1 loopback=OK",
              "LW ALIVE 2", "LW HEALTH 2 loopback=OPEN", "LW ALIVE 3", "LW HEALTH 3 loopback=OPEN"]
    for text in lines:
        t = time.time()
        b.line(t, "x", parser.parse_line(text)["records"])
        b.tick(t)
    print("OPENED", flush=True)
    n = 4
    while True:  # jumper still out: bad lines and a heartbeat every second until SIGKILL
        t = time.time()
        b.line(t, "x", parser.parse_line("LW HEALTH %%d loopback=OPEN" %% n)["records"])
        b.tick(t)
        store.heartbeat(conn, t)
        n += 1
        time.sleep(0.05)
""")


class TestKill9(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.db = os.path.join(d, "bench.db")
        self.tok = os.path.join(d, "telegram_token")
        self.chat = os.path.join(d, "telegram_chat")
        for p, v in ((self.tok, FAKE_TOKEN), (self.chat, FAKE_CHAT)):
            with open(p, "w") as f:
                f.write(v + "\n")
        self.tg = FakeTelegram()
        self.addCleanup(self.tg.close)

    def sender(self):
        conn = store.connect(self.db)
        self.addCleanup(conn.close)
        return Sender(conn, Telegram(self.tg.base, self.tok, self.chat, timeout=2),
                      out=io.StringIO())

    def test_kill9_with_open_incident_then_restart_no_resend_and_resolve(self):
        script = os.path.join(self.tmp.name, "child.py")
        with open(script, "w") as f:
            f.write(CHILD % {"repo": REPO, "db": self.db, "sha": SHA})
        p = subprocess.Popen([sys.executable, "-I", script], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.assertEqual(p.stdout.readline().strip(), b"OPENED")
        time.sleep(0.3)
        s1 = self.sender()
        self.assertEqual(s1.step(), 1)               # the first alert reached Telegram
        os.kill(p.pid, signal.SIGKILL)
        p.wait(5)
        p.stdout.close()

        conn = store.open_db(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pass_history").fetchone()[0], 1)
        logs = []
        b = Bench(conn, "nucleo-1", log=logs.append)       # the restarted bench
        self.assertTrue(any("reloaded open incident 1" in s for s in logs))
        self.assertEqual(b.m.verified_sha, SHA)
        t = time.time()
        b.apply(b.m.link_up(t), t)
        for i in range(100, 105):                          # jumper still out
            b.line(t + i * 0.01, "x", parser.parse_line("LW HEALTH %d loopback=OPEN" % i)
                   ["records"])
            b.tick(t + i * 0.01)
        s2 = self.sender()
        self.assertEqual(s2.step(), 0)                      # nothing resent
        self.assertEqual(len(self.tg.requests), 1)
        self.assertEqual([r["transition"] for r in conn.execute("SELECT transition FROM alerts")],
                         ["open"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)
        for i in range(105, 108):                          # jumper back
            b.line(t + 2 + i * 0.01, "x",
                   parser.parse_line("LW HEALTH %d loopback=OK" % i)["records"])
        inc = conn.execute("SELECT * FROM incidents").fetchone()
        self.assertEqual(inc["state"], "RESOLVED")
        self.assertIsNotNone(inc["duration_s"])
        for i in range(108, 172):                          # healthy lines, 1 Hz, past 60 s
            b.line(t + 2 + (i - 107), "x",
                   parser.parse_line("LW HEALTH %d loopback=OK" % i)["records"])
            b.tick(t + 2 + (i - 107))                       # the held resolve alert comes due
        self.assertEqual([r["transition"] for r in conn.execute(
            "SELECT transition FROM alerts ORDER BY id")], ["open", "resolve"])
        s2.step()
        self.assertEqual(len(self.tg.requests), 2)
        first = conn.execute("SELECT telegram_message_id FROM alerts WHERE "
                             "transition='open'").fetchone()[0]
        self.assertEqual(self.tg.requests[1][1]["reply_parameters"]["message_id"], first)


class TestReloadTimers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.open_db(os.path.join(self.tmp.name, "bench.db"))
        self.addCleanup(self.conn.close)

    def bench(self):
        return Bench(self.conn, "nucleo-1", log=lambda s: None)

    def test_open_link_down_resolves_after_restart(self):
        b = self.bench()
        t = time.time() - 30
        b.apply(b.m.link_up(t), t)
        b.on_event(type("E", (), {"kind": "LINK_DOWN", "t_wall": t + 1})())
        b.tick(t + 12)
        rows = self.conn.execute("SELECT kind, state FROM incidents").fetchall()
        self.assertEqual([(r["kind"], r["state"]) for r in rows], [("LINK_DOWN", "OPEN")])
        b2 = self.bench()                                   # killed while the USB was out
        b2.on_event(type("E", (), {"kind": "LINK_UP", "t_wall": t + 20})())
        b2.tick(t + 20.1)
        row = self.conn.execute("SELECT state, duration_s FROM incidents").fetchone()
        self.assertEqual(row["state"], "RESOLVED")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts WHERE "
                                           "transition='open'").fetchone()[0], 1)

    def test_flap_timer_survives_restart(self):
        b = self.bench()
        t = time.time() - 30
        b.apply(b.m.link_up(t), t)
        seq = ["OPEN", "OPEN", "OK", "OK", "OK", "OPEN", "OPEN"]
        for i, st in enumerate(seq):
            b.line(t + i, "x", parser.parse_line("LW HEALTH %d loopback=%s" % (i, st))["records"])
            b.tick(t + i)
        inc = self.conn.execute("SELECT state, flaps FROM incidents").fetchone()
        self.assertEqual((inc["state"], inc["flaps"]), ("FLAPPING", 1))
        b2 = self.bench()
        b2.apply(b2.m.link_up(t + 8), t + 8)
        for i, st in enumerate(["OK", "OK", "OK", "OPEN", "OPEN"], start=8):
            b2.line(t + i, "x", parser.parse_line("LW HEALTH %d loopback=%s" % (i, st))
                    ["records"])
            b2.tick(t + i)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT flaps FROM incidents").fetchone()[0], 2)
        trans = [r[0] for r in self.conn.execute("SELECT transition FROM alerts ORDER BY id")]
        self.assertEqual(trans, ["open", "flap-1"])          # the second flap is held (60 s)


if __name__ == "__main__":
    unittest.main()
