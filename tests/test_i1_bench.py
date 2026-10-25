"""I1: bench.run wired to requests, the watcher, decode, confirm and the DUMP collector.

No board, no GB10, no network. The board is a pty (tests.test_a_flash.FakeBoard)
playing synthetic LW text written here (invented values, protocol only); the
drive is a temp dir; git repos are temp repos with the fake Makefile; addr2line
is a fake runner.
"""

import json
import os
import tempfile
import threading
import time
import types
import unittest

from bench import flash, requests, store
from bench.incidents import Machine
from bench.run import Bench, Loop, start_watcher
from tests.test_a_flash import HAVE_SERIAL, FakeBoard, commit, make_repo
from tests.test_a_run import Rig, SHA, TESTS

GOOD_TXT = ("LW BOOT fw=0000000 reset=PIN csr=0x04000000\n" +
            "".join("LW TEST %s PASS\n" % n for n in TESTS) +
            "LW RESULT PASS\nLW ALIVE 1\n")
REG = ("LW DUMP reg r0=0x00000005 r1=0x00000006 r2=0x20000100 r3=0x00000000 "
       "r12=0x0000ffff lr=0xfffffff9 pc=0x080023a4 psr=0x21000003")
BURST = (REG, "LW DUMP stack 0x2001ff80 0x0000000a 0x0000000b 0x0000000c 0x0000000d",
         "LW DUMP cause FORCED UNDEFINSTR", "LW DUMP end")
HF = ("LW HARDFAULT pc=0x080023a4 lr=0x08001235 psr=0x21000003 cfsr=0x00010000 "
      "hfsr=0x40000000 bfar=0x00000000 mmfar=0x00000000 sp=0x2001ff80")
A2L_OUT = b"fault_here\n/src/app/main.c:42\ncaller\n/src/app/main.c:10\n"


def fake_a2l(delay=0.0, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        time.sleep(delay)
        return types.SimpleNamespace(returncode=0, stdout=A2L_OUT, stderr=b"")
    return run


class TestDecodeAndDump(unittest.TestCase):
    """The HARDFAULT paths without a pty: a hand flash verified by its BOOT line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.elf_dir = os.path.join(self.tmp.name, "elf")
        os.makedirs(self.elf_dir)
        with open(os.path.join(self.elf_dir, "%s.elf" % SHA), "w") as f:
            f.write("not really an elf")

    def rig(self, delay):
        r = Rig(self.tmp.name)
        self.addCleanup(r.conn.close)
        r.bench.elf_dir = self.elf_dir
        r.bench.decode_runner = fake_a2l(delay)
        r.bench.addr2line_tool = "/bin/true"
        r.recs = []
        rec_fn = r.bench.m.record

        def spy(t, rec, cite=None):
            r.recs.append(dict(rec))
            return rec_fn(t, rec, cite)
        r.bench.m.record = spy
        return r

    def fault_run(self, r):
        r.bench.expect_flash(SHA, t=1.0)
        r.line(2.0, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        for i, ln in enumerate(BURST):
            r.line(2.1 + 0.01 * i, ln)
        r.line(2.2, HF)

    def test_first_alert_has_fault_and_file_and_dump_reaches_store(self):
        r = self.rig(0.0)
        t0 = time.monotonic()
        self.fault_run(r)
        self.assertLess(time.monotonic() - t0, 1.0)
        al = r.rows("SELECT * FROM alerts WHERE transition='open'")
        self.assertEqual(len(al), 1)
        self.assertIn("made it crash", al[0]["text"])
        self.assertIn("Details: /src/app/main.c:42.", al[0]["text"])
        inc = r.rows("SELECT * FROM incidents")[0]
        self.assertEqual(inc["kind"], "HARDFAULT")
        ev = json.loads(inc["evidence"])
        self.assertEqual(ev["file_line"], "/src/app/main.c:42")
        run = r.rows("SELECT * FROM runs")[0]
        self.assertEqual(run["verdict"], "HARDFAULT")
        rev = json.loads(run["evidence"])
        self.assertEqual(rev["file_line"], "/src/app/main.c:42")
        # the DUMP burst: rec["dump"] on the HARDFAULT record, and stored
        hf = [x for x in r.recs if x["kind"] == "HARDFAULT"]
        self.assertTrue(hf[0]["dump"]["complete"])
        self.assertEqual(hf[0]["dump"]["cause_names"], ["FORCED", "UNDEFINSTR"])
        self.assertTrue(rev["dump"]["complete"])
        self.assertTrue(ev["dump"]["complete"])   # top level of evidence

    def test_dump_lines_discarded_while_flashing_are_not_paired(self):
        r = self.rig(0.0)
        r.bench.expect_flash(SHA, t=1.0)
        for i, ln in enumerate(BURST):      # the old build's burst: discarded by the gate
            r.line(1.1 + 0.01 * i, ln)
        r.line(2.0, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        r.line(2.2, HF)
        hf = [x for x in r.recs if x["kind"] == "HARDFAULT" and "dump" in x]
        self.assertEqual(hf, [])

    def test_slow_addr2line_sends_alert_first_then_decode(self):
        r = self.rig(0.8)
        t0 = time.monotonic()
        self.fault_run(r)
        self.assertLess(time.monotonic() - t0, 0.7)
        al = r.rows("SELECT * FROM alerts WHERE transition='open'")
        self.assertNotIn("File:", al[0]["text"])
        end = time.monotonic() + 3
        while time.monotonic() < end and not r.rows(
                "SELECT * FROM alerts WHERE transition='decode'"):
            r.bench.tick(3.0)
            time.sleep(0.05)
        dec = r.rows("SELECT * FROM alerts WHERE transition='decode'")
        self.assertEqual(len(dec), 1)
        self.assertIn("Found where the code crashed", dec[0]["text"])
        self.assertIn("Details: /src/app/main.c:42", dec[0]["text"])
        ev = json.loads(r.rows("SELECT evidence FROM incidents")[0]["evidence"])
        self.assertEqual(ev["file_line"], "/src/app/main.c:42")


class TestDeviceEvidence(unittest.TestCase):
    """I2's per-device incidents opened through bench.run keep evidence.device at the top."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = Rig(self.tmp.name)
        self.addCleanup(self.r.conn.close)

    def health(self, t, n, **dev):
        fields = {"sensor": "OK", "radio": "OK", "estop": "OFF"}
        fields.update(dev)
        self.r.line(t, "LW ALIVE %d" % n)
        self.r.line(t + 0.01, "LW HEALTH %d loopback=OK%s" % (
            n, "".join(" %s=%s" % kv for kv in fields.items())))

    def test_estop_and_sensor_open_have_device(self):
        self.health(1, 1)
        self.health(2, 2, estop="ON")
        self.health(3, 3, sensor="OPEN")
        self.health(4, 4, sensor="OPEN")
        inc = {i["kind"]: json.loads(i["evidence"])
               for i in self.r.rows("SELECT * FROM incidents")}
        self.assertEqual(inc["ESTOP"]["device"], "estop")
        sensor = [ev for k, ev in inc.items() if k != "ESTOP"]
        self.assertEqual([ev["device"] for ev in sensor], ["sensor"])


class TestTestRequest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = Rig(self.tmp.name)
        self.addCleanup(self.r.conn.close)
        self.r.bench.make_flash = lambda sha: self.fail("no flash expected")

    def test_test_request_sent_after_result_and_alive_and_finished_on_reply(self):
        r = self.r
        rid = requests.add(r.conn, 1.0, "endpoint", "test", "crc32")
        r.conn.commit()
        r.bench.tick(1.5)        # no RESULT + ALIVE yet: refused, not sent
        self.assertEqual(r.sent, [])
        row = r.rows("SELECT * FROM bench_requests WHERE id=?", rid)[0]
        self.assertEqual(row["state"], "failed")
        r.good_run(2.0)
        r.line(2.5, "LW ALIVE 1")
        rid = requests.add(r.conn, 3.0, "endpoint", "test", "crc32")
        r.conn.commit()
        r.bench.tick(3.1)
        self.assertEqual(r.sent, ["test crc32"])
        r.line(3.2, "LW REPLY test crc32 PASS")
        row = r.rows("SELECT * FROM bench_requests WHERE id=?", rid)[0]
        self.assertEqual(row["state"], "done")
        self.assertIn("LW REPLY test crc32", row["result"])
        rid = requests.add(r.conn, 4.0, "endpoint", "test", "ringbuf")
        r.conn.commit()
        r.bench.tick(4.1)
        r.bench.tick(7.2)        # no reply in 3 s
        row = r.rows("SELECT * FROM bench_requests WHERE id=?", rid)[0]
        self.assertEqual((row["state"], row["result"]), ("failed", "no LW REPLY test within 3 s"))

    def test_running_request_failed_at_startup(self):
        r = self.r
        rid = requests.add(r.conn, 1.0, "endpoint", "run", "a" * 40)
        r.conn.commit()
        requests.claim_next(r.conn, 1.1)
        Bench(r.conn, "nucleo-1", log=r.logs.append)
        row = r.rows("SELECT * FROM bench_requests WHERE id=?", rid)[0]
        self.assertEqual(row["state"], "failed")


@unittest.skipUnless(HAVE_SERIAL, "pyserial not importable")
class TestPty(unittest.TestCase):
    def setUp(self):
        from bench.serial_owner import SerialOwner
        from tests.test_a_serial_owner import FakePort
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.good_raw = os.path.join(d, "good.txt")
        with open(self.good_raw, "w") as f:
            f.write(GOOD_TXT)
        self.repo = os.path.join(d, "bench-fw")
        self.good_full = make_repo(self.repo, {"src/a.c": "int a;\n"})
        self.good = self.good_full[:7]
        self.state = os.path.join(d, "state")
        self.drive = os.path.join(d, "NOD_F446RE")
        os.makedirs(self.drive)
        self.port = FakePort(os.path.join(d, "usb-STMicroelectronics_STLINK-V2-1_TEST-if02"))
        self.addCleanup(self.port.unplug)
        self.owner = SerialOwner(port_glob=os.path.join(d, "*STM*"), idle_flush=0.2,
                                 reopen_interval=0.1).start()
        self.addCleanup(self.owner.stop)
        self.db = os.path.join(d, "bench.db")
        self.conn = store.open_db(self.db)
        self.addCleanup(self.conn.close)
        self.logs = []
        self.bench = Bench(self.conn, "nucleo-1", log=self.logs.append, fault_tail_s=0.5,
                           machine=Machine("nucleo-1", capture_s=6.0), repo=self.repo,
                           elf_dir=os.path.join(self.state, "elf"))
        self.loop = Loop(self.bench, self.owner.events, self.conn)
        self.bench.make_flash = lambda sha: flash.FlashJob(
            self.bench, self.loop.call, self.repo, sha, self.state,
            drive=flash.Drive(fixed=self.drive), window=2.0, sync=lambda: None,
            log=self.logs.append).start()

    def board(self, plan):
        b = FakeBoard(self.port, self.drive, plan)
        b.start()
        self.addCleanup(b.stop.set)
        return b

    def rows(self, sql, *a):
        return [dict(r) for r in self.conn.execute(sql, a).fetchall()]

    def step_until(self, cond, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.loop.step(0.05)
            if cond():
                return
        self.fail("timed out: %s" % self.logs[-8:])

    def request_done(self, rid):
        return lambda: self.rows("SELECT state FROM bench_requests WHERE id=?",
                                 rid)[0]["state"] in ("done", "failed")

    def test_run_request_flashes_runs_and_finishes(self):
        self.board([self.good_raw])
        self.step_until(lambda: self.bench.m.link, 5)
        rid = requests.add(self.conn, time.time(), "endpoint", "run", self.good_full)
        self.conn.commit()
        self.step_until(self.request_done(rid))
        req = self.rows("SELECT * FROM bench_requests WHERE id=?", rid)[0]
        runs = self.rows("SELECT * FROM runs")
        self.assertEqual([(r["commit"], r["verdict"], r["subject"]) for r in runs],
                         [(self.good, "PASS", "first")])
        self.assertEqual((req["state"], req["result"], req["run_id"]),
                         ("done", "PASS", runs[0]["run_id"]))

    def test_watcher_queues_new_head_which_is_flashed_and_recorded(self):
        self.board([self.good_raw])
        self.step_until(lambda: self.bench.m.link, 5)
        stop = threading.Event()
        self.addCleanup(stop.set)
        start_watcher(self.db, self.repo, stop, interval=0.2, log=self.logs.append)
        self.step_until(lambda: self.rows("SELECT * FROM watcher_state"), 5)
        self.assertEqual(self.rows("SELECT * FROM bench_requests"), [])  # first start
        new = commit(self.repo, "second change", {"src/a.c": "int a = 2;\n"})
        self.step_until(lambda: [r for r in self.rows("SELECT * FROM bench_requests")
                                 if r["state"] == "done"])
        req = self.rows("SELECT * FROM bench_requests")
        self.assertEqual([(r["source"], r["arg"], r["result"]) for r in req],
                         [("watcher", new, "PASS")])
        runs = self.rows("SELECT * FROM runs")
        self.assertEqual([(r["commit"], r["verdict"], r["subject"]) for r in runs],
                         [(new[:7], "PASS", "second change")])
        self.assertEqual(self.rows("SELECT verified_sha FROM bench_state")[0]["verified_sha"],
                         new[:7])

    def test_no_output_then_confirm_reflashes_last_pass_and_says_code(self):
        bad_full = commit(self.repo, "silent change", {"src/a.c": "int a = 3;\n"})
        self.board([self.good_raw, None, self.good_raw])
        self.step_until(lambda: self.bench.m.link, 5)
        rid = requests.add(self.conn, time.time(), "endpoint", "run", self.good_full)
        self.conn.commit()
        self.step_until(self.request_done(rid))
        rid = requests.add(self.conn, time.time(), "endpoint", "run", bad_full)
        self.conn.commit()
        self.step_until(lambda: self.rows("SELECT * FROM alerts WHERE transition='confirmed'"),
                        30)
        req = self.rows("SELECT * FROM bench_requests WHERE id=?", rid)[0]
        self.assertEqual((req["state"], req["result"]), ("failed", "NO_OUTPUT"))
        runs = self.rows("SELECT * FROM runs ORDER BY run_id")
        self.assertEqual([(r["commit"], r["verdict"]) for r in runs],
                         [(self.good, "PASS"), (None, "NO_OUTPUT"), (self.good, "PASS")])
        self.assertEqual(runs[1]["triage"], "CODE")
        inc = self.rows("SELECT * FROM incidents")
        self.assertEqual([(i["kind"], i["triage"]) for i in inc], [("RUN_FAIL", "CODE")])
        al = self.rows("SELECT * FROM alerts WHERE transition='confirmed'")[0]
        self.assertIn("It is the code, not the board.", al["text"])
        self.assertEqual(self.rows("SELECT verified_sha FROM bench_state")[0]["verified_sha"],
                         self.good)
        self.assertFalse(self.bench.confirming)


if __name__ == "__main__":
    unittest.main()
