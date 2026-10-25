"""Lane S: the read-only status view (bench/status_view.py), --once frames and a pty run."""

import contextlib
import fcntl
import io
import json
import os
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest

from bench import status_view
from bench.status_data import Db, LogTail, clean, open_ro
from stub import emit, make_db
from stub.db import connect

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_emit(db, scenario):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        code = emit.main(["--db", db, "--scenario", scenario, "--speed", "0"])
    assert code == 0, scenario


def once(*args, width=120, height=60):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = status_view.main(["--once", "--width", str(width), "--height", str(height)]
                                + list(args))
    assert code == 0
    return out.getvalue()


def fmt(dt):
    return "+%.1f s" % dt


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "bench.db")

    def tearDown(self):
        self.tmp.cleanup()

    def frame(self, *extra, **kw):
        return once("--db", self.db, "--no-color", *extra, **kw)

    def rw(self):
        return connect(self.db)


class ClassifyTest(unittest.TestCase):
    def test_classes(self):
        c = status_view.classify
        self.assertEqual(c("09:00:00.000 LW HEALTH 3 loopback=OK"), "ok")
        self.assertEqual(c("LW HEALTH 3 loopback=OPEN"), "bad")
        self.assertEqual(c("LW HEALTH 3 loopback=STUCK"), "bad")
        self.assertEqual(c("LW HARDFAULT pc=0x1 cfsr=0x2"), "bad")
        self.assertEqual(c("LW RESULT FAIL"), "bad")
        self.assertEqual(c("12:00:00.000 LW DUMP 0x20000000 00 11"), "bad")
        self.assertEqual(c("LW RESULT PASS"), "plain")
        self.assertEqual(c("LW TEST crc32 PASS"), "plain")
        self.assertEqual(c("free text from the board"), "plain")

    def test_clean_strips_escapes_and_controls(self):
        s = clean("\x1b[31mLW\x1b]0;title\x07 HEALTH\x00\x07\r 1\x9b2J loopback=OK\x1b[0m\xe9")
        self.assertEqual(s, "LW HEALTH 1 loopback=OK?")


class CannedDbTest(Base):
    def setUp(self):
        super().setUp()
        make_db.build(self.db)

    def test_green_and_red_lines(self):
        f = self.frame()
        lines = f.splitlines()
        self.assertTrue(any(l.startswith("+ ") and "loopback=OK" in l for l in lines))
        self.assertTrue(any(l.startswith("! ") and "loopback=OPEN" in l for l in lines))
        self.assertTrue(any(l.startswith("! ") and "LW HARDFAULT" in l for l in lines))
        self.assertTrue(any(l.startswith("  ") and "LW TEST crc32 PASS" in l for l in lines))
        self.assertIn("model: Qwen3.6-35B-A3B on this GB10 - no internet", f)

    def test_ansi_colors(self):
        f = once("--db", self.db)
        self.assertIn("\x1b[32m+ ", f)       # green HEALTH OK
        self.assertIn("\x1b[31m! ", f)       # red fault line
        self.assertIn("\x1b[1;97;41m  >>> CODE: HardFault at src/demo.c:42", f)

    def test_code_banner_with_bits(self):
        f = self.frame()
        self.assertIn(">>> CODE: HardFault at src/demo.c:42 <<<", f)
        self.assertIn("CFSR DACCVIOL MMARVALID", f)
        self.assertIn("HFSR FORCED", f)
        self.assertIn("FAULT: 2 open incidents", f)

    def test_last_sent_text(self):
        c = self.rw()
        r = c.execute("SELECT * FROM alerts WHERE sent IS NOT NULL "
                      "ORDER BY sent DESC, id DESC LIMIT 1").fetchone()
        c.close()
        f = self.frame()
        sec = f.split("LAST TELEGRAM SENT", 1)[1]
        self.assertIn("incident %d, %s]" % (r["incident_id"], r["transition"]), sec)
        self.assertIn(r["text"].splitlines()[0], sec)

    def test_read_only_connection(self):
        conn = open_ro(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO events (t_wall, board, kind, raw_ts) "
                         "VALUES (1, 'x', 'ALIVE', '00:00:00.000')")
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM incidents")
        conn.close()
        d = Db(self.db)
        self.assertEqual(d.read()["error"], None)
        d.close()

    def test_survives_concurrent_wal_writes(self):
        stop = threading.Event()

        def writer():
            c = self.rw()
            n = 0
            while not stop.is_set():
                with c:
                    c.execute("INSERT INTO events (t_wall, board, kind, fields, raw_ts) "
                              "VALUES (?, 'nucleo-1', 'HEALTH', ?, '10:00:00.000')",
                              (time.time(), json.dumps({"line": "LW HEALTH %d loopback=OK" % n})))
                n += 1
            c.close()

        th = threading.Thread(target=writer)
        th.start()
        view = status_view.View(self.db)
        try:
            for _ in range(40):
                rows = view.rows(100, 40)
                self.assertNotIn("! DB", rows[1][0])
                time.sleep(0.01)
            stop.set()
            th.join()
            self.assertIn("loopback=OK", view.rows(100, 40)[-2][0])
        finally:
            stop.set()
            th.join()
            view.close()

    def test_small_and_resized_frames(self):
        view = status_view.View(self.db)
        try:
            for w, h in ((20, 6), (40, 10), (200, 80), (80, 24)):
                rows = view.rows(w, h)
                self.assertEqual(len(rows), h)
                self.assertTrue(all(len(t) <= max(w, 20) for t, _ in rows))
        finally:
            view.close()


class LiveScenarioTest(Base):
    def test_jumper_banner_and_stopwatch(self):
        run_emit(self.db, "jumper")
        f = self.frame()
        self.assertIn(">>> HARDWARE: loopback OPEN on A0-A1/PA1 <<<", f)
        c = self.rw()
        inc = c.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        row = [l for l in f.splitlines() if l.strip().startswith("#%d " % inc["id"])][0]
        self.assertIn("alert %s" % fmt(inc["t_alert_sent"] - inc["t_first_bad_line"]), row)
        self.assertIn("fix PR n/a", row)

    def test_hardfault_stopwatch_explain_and_fix(self):
        run_emit(self.db, "hardfault")
        c = self.rw()
        inc = c.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 1").fetchone()
        t0 = inc["t_first_bad_line"]
        f = self.frame()
        self.assertIn(">>> CODE: HardFault at src/demo.c:42 <<<", f)
        self.assertIn("explain waiting", f)
        with c:
            c.execute("INSERT INTO alerts (incident_id, transition, text, created, sent) "
                      "VALUES (?, 'explain', ?, ?, ?)",
                      (inc["id"], "LAB WATCH incident %d explanation\nDIAGNOSIS: x" % inc["id"],
                       t0 + 9.0, t0 + 12.34))
            c.execute("INSERT INTO alerts (incident_id, transition, text, created, sent) "
                      "VALUES (?, 'fix', ?, ?, ?)",
                      (inc["id"], "LAB WATCH incident %d fix PR #7" % inc["id"], t0 + 80.0,
                       t0 + 81.25))
        c.close()
        f = self.frame()
        row = [l for l in f.splitlines() if l.strip().startswith("#%d " % inc["id"])][0]
        self.assertIn("alert %s" % fmt(inc["t_alert_sent"] - t0), row)
        self.assertIn("explain +12.3 s", row)
        self.assertIn("fix PR +81.2 s", row)
        self.assertIn("LAB WATCH incident %d fix PR #7" % inc["id"],
                      f.split("LAST TELEGRAM SENT", 1)[1])

    def test_decode_keys_read_defensively(self):
        run_emit(self.db, "hardfault")
        c = self.rw()
        ev = {"registers": {"pc": "0x08000100"}, "decode": {"cfsr_bits": [{"name": "IACCVIOL"}],
                                                            "file_line": "src/x.c:7"},
              "hfsr_bits": "FORCED", "lines": "junk"}
        with c:
            c.execute("UPDATE incidents SET evidence=?", (json.dumps(ev),))
        f = self.frame()
        self.assertIn("CODE: HardFault at src/x.c:7", f)
        self.assertIn("CFSR IACCVIOL", f)
        with c:
            c.execute("UPDATE incidents SET evidence='not json'")
        c.close()
        self.assertIn("CODE: HardFault", self.frame())

    def test_recover_turns_clear(self):
        run_emit(self.db, "jumper")
        run_emit(self.db, "recover")
        f = self.frame()
        self.assertIn("ALL CLEAR", f)
        self.assertNotIn(">>>", f)


class RobustnessTest(Base):
    def test_missing_db(self):
        f = self.frame()
        self.assertIn("DB not found", f)
        self.assertIn("ALL CLEAR", f)
        self.assertFalse(os.path.exists(self.db))  # read-only: never created

    def test_empty_db(self):
        sqlite3.connect(self.db).close()
        f = self.frame()
        self.assertIn("no bench tables yet", f)
        self.assertIn("model: Qwen3.6-35B-A3B", f)

    def test_schema_only_db(self):
        c = self.rw()
        with open(os.path.join(REPO, "docs", "schema.sql")) as s:
            c.executescript(s.read())
        c.close()
        f = self.frame()
        self.assertIn("no incidents yet", f)
        self.assertIn("nothing sent yet", f)
        self.assertIn("no serial lines", f)

    def test_missing_log(self):
        make_db.build(self.db)
        f = self.frame("--log", os.path.join(self.tmp.name, "nope.log"))
        self.assertIn("log file not found", f)
        self.assertIn("CODE: HardFault", f)

    def test_log_junk_and_controls(self):
        log = os.path.join(self.tmp.name, "bench.log")
        with open(log, "wb") as fh:
            fh.write(b"\x1b[31m10:00:00.000 LW HEALTH 1 loopback=OK\x1b[0m\x07\r\n"
                     b"\xff\xfe\x00garbage \x1b]0;pwned\x07 line\n"
                     b"10:00:01.000 LW HEALTH 2 loopback=STUCK\r"
                     b"\x1b[2J\x1b[H LW RESULT FAIL\n" + b"A" * 9000 + b"\n")
        for color in ([], ["--no-color"]):
            f = once("--db", self.db, "--log", log, *color)
            body = f.replace("\x1b[0m", "")
            for code in status_view.ANSI.values():
                if code:
                    body = body.replace(code, "")
            self.assertNotIn("\x1b", body)
            self.assertFalse(any(ord(ch) < 32 and ch != "\n" for ch in body))
        f = once("--db", self.db, "--log", log, "--no-color")
        self.assertIn("+ 10:00:00.000 LW HEALTH 1 loopback=OK", f)
        self.assertIn("! 10:00:01.000 LW HEALTH 2 loopback=STUCK", f)
        self.assertIn("!  LW RESULT FAIL", f)
        self.assertIn("garbage  line", f)

    def test_event_line_with_controls(self):
        make_db.build(self.db)
        c = self.rw()
        with c:
            c.execute("INSERT INTO events (t_wall, board, kind, fields, raw_ts) VALUES "
                      "(?, 'nucleo-1', 'OTHER', ?, '\x1b[5m11:00:00.000')",
                      (time.time(), json.dumps({"line": "LW \x1b[41mHEALTH\x00 9 loopback=OPEN"})))
            c.execute("INSERT INTO events (t_wall, board, kind, fields, raw_ts) VALUES "
                      "(?, 'nucleo-1', 'OTHER', ?, 'x')", (time.time(), "[1, 2]"))
        c.close()
        f = self.frame()
        self.assertIn("! 11:00:00.000 LW HEALTH 9 loopback=OPEN", f)
        self.assertNotIn("\x1b", f)

    def test_log_tail_follows_and_truncation(self):
        log = os.path.join(self.tmp.name, "b.log")
        t = LogTail(log)
        t.poll()
        self.assertIn("not found", t.note)
        with open(log, "w") as fh:
            fh.write("LW HEALTH 1 loopback=OK\nLW HEA")
        t.poll()
        self.assertEqual(list(t.lines), ["LW HEALTH 1 loopback=OK"])
        with open(log, "a") as fh:
            fh.write("LTH 2 loopback=OPEN\n")
        t.poll()
        self.assertEqual(list(t.lines)[-1], "LW HEALTH 2 loopback=OPEN")
        with open(log, "w") as fh:
            fh.write("LW BOOT fw=abc\n")
        t.poll()
        self.assertEqual(list(t.lines)[-1], "LW BOOT fw=abc")
        t.close()


class PtyTest(Base):
    """The full-screen view in a real pty: turns red within 1 s, resizes, q quits."""

    def spawn(self, *args):
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(REPO)
            env = dict(os.environ, TERM="xterm", PYTHONPATH=REPO)
            os.execvpe(sys.executable, [sys.executable, "-m", "bench.status_view",
                                        "--db", self.db] + list(args), env)
        self.set_size(fd, 30, 110)
        return pid, fd

    @staticmethod
    def set_size(fd, rows, cols):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    @staticmethod
    def read_until(fd, needle, timeout):
        buf, end = b"", time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                try:
                    buf += os.read(fd, 65536)
                except OSError:
                    break
                if needle in buf:
                    return buf, time.time()
        return buf, None

    def wait_exit(self, pid, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            p, status = os.waitpid(pid, os.WNOHANG)
            if p:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        self.fail("status view did not exit")

    def test_red_within_one_second_then_q(self):
        c = self.rw()
        with open(os.path.join(REPO, "docs", "schema.sql")) as s:
            c.executescript(s.read())
        c.close()
        pid, fd = self.spawn()
        try:
            _, seen = self.read_until(fd, b"ALL CLEAR", 5.0)
            self.assertIsNotNone(seen, "first frame")
            run_emit(self.db, "jumper")
            c = self.rw()
            t_row = c.execute("SELECT t_open FROM incidents").fetchone()[0]
            c.close()
            _, seen = self.read_until(fd, b"HARDWARE:", 3.0)
            self.assertIsNotNone(seen, "banner")
            self.assertLess(seen - t_row, 1.0)
            self.set_size(fd, 20, 70)
            os.kill(pid, signal.SIGWINCH)
            time.sleep(0.3)
            os.write(fd, b"q")
            self.assertEqual(self.wait_exit(pid), 0)
        finally:
            os.close(fd)

    def test_ctrl_c_exits_cleanly(self):
        make_db.build(self.db)
        pid, fd = self.spawn("--no-color")
        try:
            _, seen = self.read_until(fd, b"LAB WATCH", 5.0)
            self.assertIsNotNone(seen)
            os.write(fd, b"\x03")
            self.assertEqual(self.wait_exit(pid), 0)
        finally:
            os.close(fd)


class CliTest(Base):
    def test_once_subprocess(self):
        make_db.build(self.db)
        p = subprocess.run([sys.executable, "-m", "bench.status_view", "--db", self.db,
                            "--once", "--no-color"], cwd=REPO, capture_output=True, text=True,
                           timeout=30, env=dict(os.environ, COLUMNS="100", LINES="40"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(p.stdout.splitlines()), 40)
        self.assertIn("CODE: HardFault at src/demo.c:42", p.stdout)


if __name__ == "__main__":
    unittest.main()
