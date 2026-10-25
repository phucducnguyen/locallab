"""Lane E: fleet build commands, boards.json, restarts, and soak counting.

No QEMU needed: fleet.fakeqemu stands in for qemu-system-arm. The pty soak
test needs pyserial and skips cleanly without it.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tty
import unittest
from types import SimpleNamespace

try:
    import serial  # noqa: F401
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False

from fleet import build, run, soak

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = "%s -m fleet.fakeqemu" % sys.executable


class FakeSP:
    """Stands in for the subprocess module inside fleet.build."""

    def __init__(self, mark=b"SIM build"):
        self.calls = []
        self.mark = mark

    def run(self, cmd, stdin=None, **kw):
        self.calls.append(cmd)
        out = b""
        if cmd[3:5] == ["rev-parse", "--verify"]:
            out = b"abc1234" + b"0" * 33 + b"\n"
        elif cmd[3:5] == ["rev-parse", "--short=7"]:
            out = b"abc1234\n"
        elif cmd[0] == "make":
            d = os.path.join(cmd[2], "build")
            os.makedirs(d)
            with open(os.path.join(d, "fw.elf"), "wb") as f:
                f.write(b"\x7fELF...LW BOOT..." + self.mark + b"...")
            with open(os.path.join(d, "fw.bin"), "wb") as f:
                f.write(b"real-board image")
        return SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    def Popen(self, cmd, **kw):
        self.calls.append(cmd)
        return SimpleNamespace(stdout=None, wait=lambda: 0)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "out")

    def test_commands_and_output(self):
        sp = FakeSP()
        path = build.build("/x/bench-fw", "HEAD", self.out, toolchain="/t/arm-none-eabi-", sp=sp)
        self.assertEqual(path, os.path.join(self.out, "elf", "abc1234.sim.elf"))
        full = "abc1234" + "0" * 33
        archive = [c for c in sp.calls if "archive" in c][0]
        self.assertEqual(archive, ["git", "-C", "/x/bench-fw", "archive", "--format=tar", full])
        tar = [c for c in sp.calls if c[0] == "tar"][0]
        self.assertEqual(tar[:3], ["tar", "-x", "-C"])
        tmpdir = tar[3]
        make = [c for c in sp.calls if c[0] == "make"][0]
        self.assertEqual(make, ["make", "-C", tmpdir, "SIM=1", "FW_VERSION=abc1234",
                                "TOOLCHAIN=/t/arm-none-eabi-", "build/fw.elf"])
        self.assertFalse(os.path.exists(tmpdir), "temp build dir is removed")
        with open(path, "rb") as f:
            self.assertIn(b"SIM build", f.read())
        found = [n for _, _, fs in os.walk(self.out) for n in fs]
        self.assertEqual(found, ["abc1234.sim.elf"])  # never a .bin
        # the repo is only ever read through git; nothing builds inside it
        for c in sp.calls:
            if c[0] == "make":
                self.assertNotIn("/x/bench-fw", c)

    def test_refuses_non_sim_elf(self):
        with self.assertRaises(build.BuildError):
            build.build("/x/r", "HEAD", self.out, toolchain="", sp=FakeSP(mark=b"plain"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "elf")))

    def test_bad_ref(self):
        for ref in ("--output=/tmp/x", "", "a b", "HEAD;rm"):
            with self.assertRaises(build.BuildError):
                build.build("/x/r", ref, self.out, sp=FakeSP())

    def test_make_cmd_without_toolchain(self):
        self.assertEqual(build.make_cmd("/t", "abc1234"),
                         ["make", "-C", "/t", "SIM=1", "FW_VERSION=abc1234", "build/fw.elf"])


class ArgvTest(unittest.TestCase):
    def test_qemu_argv(self):
        a = run.qemu_argv(["qemu-system-arm"], "/e/x.sim.elf")
        self.assertEqual(a, ["qemu-system-arm", "-M", "netduinoplus2", "-display", "none",
                             "-monitor", "none", "-serial", "null", "-serial", "pty",
                             "-kernel", "/e/x.sim.elf"])
        a = run.qemu_argv(["q"], "/e/x.sim.elf", icount="4", monitor="/s/emu-1.mon")
        self.assertIn("unix:/s/emu-1.mon,server=on,wait=off", a)
        self.assertEqual(a[-2:], ["-icount", "shift=4"])

    def test_find_pty(self):
        txt = ("qemu-system-arm: -serial pty: char device redirected to /dev/pts/9 "
               "(label serial1)\n")
        self.assertEqual(run.find_pty(txt), "/dev/pts/9")
        self.assertIsNone(run.find_pty("nothing yet"))

    def test_refuses_plain_elf(self):
        with tempfile.TemporaryDirectory() as d:
            elf = os.path.join(d, "abc1234.elf")
            open(elf, "w").close()
            r = subprocess.run([sys.executable, "-m", "fleet.run", "--elf", elf,
                                "--boards", "1", "--state-dir", d], cwd=REPO,
                               capture_output=True, timeout=20)
            self.assertEqual(r.returncode, 2)
            self.assertFalse(os.path.exists(os.path.join(d, "boards.json")))


def wait_until(fn, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in %.0fs" % timeout)


class FleetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.elf = os.path.join(self.tmp.name, "abc1234.sim.elf")
        open(self.elf, "w").close()
        self.state = os.path.join(self.tmp.name, "run")
        self.bj = os.path.join(self.state, "boards.json")

    def read(self):
        try:
            with open(self.bj) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def test_boards_json_restart_and_sigterm(self):
        p = subprocess.Popen([sys.executable, "-m", "fleet.run", "--elf", self.elf,
                              "--boards", "3", "--state-dir", self.state, "--qemu", FAKE],
                             cwd=REPO, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        data = wait_until(lambda: (self.read() or {}).get("boards") and self.read())
        self.assertEqual(data["fleet"]["state"], "running")
        self.assertEqual(data["fleet"]["pid"], p.pid)
        boards = data["boards"]
        self.assertEqual([b["board"] for b in boards], ["emu-1", "emu-2", "emu-3"])
        for b in boards:
            self.assertIs(b["emulated"], True)
            self.assertEqual(b["elf"], self.elf)
            self.assertTrue(b["pty"].startswith("/dev/pts/"), b["pty"])
            self.assertEqual(os.readlink(b["link"]), b["pty"])
            self.assertIsInstance(b["pid"], int)
            self.assertEqual(b["restarts"], 0)
            self.assertNotIn("monitor", b)
        old = boards[1]
        os.kill(old["pid"], signal.SIGKILL)

        def restarted():
            d = self.read()
            b = d and d["boards"][1]
            return b if b and b["restarts"] == 1 and b["pid"] and b["pid"] != old["pid"] else None
        b = wait_until(restarted)
        self.assertEqual(os.readlink(b["link"]), b["pty"])
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(10), 0)
        data = self.read()
        self.assertEqual(data["fleet"]["state"], "stopped")
        for b in data["boards"]:
            self.assertIsNone(b["pid"])
            self.assertFalse(os.path.lexists(b["link"]))
        for b in boards:
            self.assertFalse(run.pid_alive(b["pid"]) and _not_zombie(b["pid"]))


def _not_zombie(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def ev(kind="LINE", t=0.0, w=0.0, recs=(), nonlw=False, bad=0):
    return SimpleNamespace(kind=kind, t_mono=t, t_wall=w, records=list(recs), nonlw=nonlw,
                           bad_bytes=bad)


def rec(line):
    from bench import parser
    return parser.parse_line(line)["records"]


class CountTest(unittest.TestCase):
    """BoardSoak alone, fed with synthetic events (no pyserial)."""

    def test_counts_and_gap(self):
        s = soak.BoardSoak("emu-1", 100.0, 1000.0, expect_boot=True)
        s.on_event(ev("LINK_UP", 100.0))
        s.on_event(ev(t=100.1, w=1000.1, recs=rec("LW BOOT fw=abc1234 reset=POR csr=0x0")))
        s.on_event(ev(t=100.1, w=1000.1, nonlw=True))  # banner: fine before HEALTH
        s.on_event(ev(t=100.2, w=1000.2, recs=rec("LW RESULT PASS")))
        s.on_event(ev(t=100.3, w=1000.3, recs=rec("LW ALIVE 1LW HEALTH 1 loopback=OK")))
        s.on_event(ev(t=104.0, w=1004.0, recs=rec("LW ALIVE 2")))  # 3.7 s gap
        s.on_event(ev(t=104.0, w=1004.0, recs=rec("LW HEALTH 2 loopback=OK")))
        out = s.summary(105.0)
        self.assertEqual(out["by_kind"], {"ALIVE": 2, "BOOT": 1, "HEALTH": 2, "RESULT": 1})
        self.assertEqual(out["lw_lines"], 5)
        self.assertEqual(out["nonlw_lines"], 1)
        self.assertEqual(out["boot_seen"], 1)
        self.assertEqual(out["result"], "PASS")
        self.assertEqual(len(out["gaps"]), 1)
        self.assertAlmostEqual(out["gaps"][0]["gap_s"], 3.7, places=1)
        self.assertEqual(out["health_per_s_max"], 1)
        self.assertEqual(out["health_per_s_min"], 0)
        self.assertEqual(len(out["anomalies"]), 1, out["anomalies"])

    def test_anomalies(self):
        s = soak.BoardSoak("emu-2", 0.0, 0.0)
        s.on_event(ev(t=0.5, recs=rec("LW HEALTH 5 loopback=OPEN")))
        s.on_event(ev(t=0.6, nonlw=True, bad=2))
        s.on_event(ev(t=0.7, recs=rec("LW HEALTH 3 loopback=OK")))
        s.on_event(ev(t=0.8, recs=rec("LW HARDFAULT pc=0x1")))
        s.on_event(ev("LINK_UP", 0.0))
        s.on_event(ev("LINK_DOWN", 0.9))
        a = " | ".join(s.summary(5.0)["anomalies"])
        for want in ("gap>3s", "unreadable bytes 2", "non-LW lines after HEALTH 1",
                     "HEALTH not OK 1", "HARDFAULT", "counter went back 1", "link down 1"):
            self.assertIn(want, a)

    def test_banner_after_health_is_boot_text(self):
        s = soak.BoardSoak("emu-4", 0.0, 0.0)
        s.on_event(ev(t=0.1, recs=rec("LW HEALTH 7 loopback=OK")))
        s.on_event(ev(t=0.2, nonlw=True))  # banner of a reset
        s.on_event(ev(t=0.2, recs=rec("LW BOOT fw=abc1234 reset=NONE csr=0x00000000")))
        s.on_event(ev(t=0.3, recs=rec("LW ALIVE 1LW HEALTH 1 loopback=OK")))
        out = s.summary(1.0)
        self.assertEqual(out["anomalies"], [])
        self.assertEqual(out["nonlw_lines"], 1)

    def test_silent_board(self):
        out = soak.BoardSoak("emu-3", 0.0, 0.0, expect_boot=True).summary(4.0)
        self.assertTrue(out["gaps"][0]["open"])
        self.assertIn("no HEALTH", out["anomalies"])
        self.assertIn("no BOOT", out["anomalies"])


@unittest.skipUnless(HAVE_SERIAL, "pyserial not importable")
class PtySoakTest(unittest.TestCase):
    """soak.soak() with lane A's SerialOwner on a pty fed with synthetic lines."""

    def test_pty_feed(self):
        with tempfile.TemporaryDirectory() as d:
            ports = []
            boards = []
            for name in ("emu-1", "emu-2"):
                m, s = os.openpty()
                tty.setraw(m)
                ports.append((m, s))
                link = os.path.join(d, name + ".tty")
                os.symlink(os.ttyname(s), link)
                boards.append({"board": name, "emulated": True, "pty": os.ttyname(s),
                               "link": link, "pid": None, "elf": "x.sim.elf"})
            boards.append({"board": "real", "emulated": False, "pty": "/dev/null"})
            with open(os.path.join(d, "boards.json"), "w") as f:
                json.dump({"fleet": {}, "boards": boards}, f)

            def feed(m, script):
                for delay, data in script:
                    time.sleep(delay)
                    os.write(m, data)

            clean = [(0.5, b"LW ALIVE %d\r\nLW HEALTH %d loopback=OK\r\n" % (i, i))
                     for i in range(1, 12)]
            bad = [(0.5, b"LW ALIVE 1\r\nLW HEALTH 1 loopback=OK\r\n"),
                   (0.3, b"LW ALIVE 2LW HEALTH 2 loopback=OK\r\n"),  # glued
                   (3.6, b"LW ALIVE 3\r\n"),  # after a 3.6 s gap
                   (0.2, b"LW HEALTH 3 loopback=OK\r\nnoise \xff\r\n")]
            ts = [threading.Thread(target=feed, args=(ports[0][0], clean)),
                  threading.Thread(target=feed, args=(ports[1][0], bad))]
            for t in ts:
                t.start()
            out = open(os.devnull, "w")
            try:
                res = soak.soak(d, 6.5, out=out)
            finally:
                out.close()
                for t in ts:
                    t.join()
                for m, s in ports:
                    os.close(m)
                    os.close(s)
        self.assertEqual(res["boards"], 2)  # the real board is never opened
        a, b = res["per_board"]
        self.assertEqual(a["by_kind"], {"ALIVE": 11, "HEALTH": 11})
        self.assertEqual(a["anomalies"], [])
        self.assertEqual(b["by_kind"], {"ALIVE": 3, "HEALTH": 3})
        self.assertEqual(b["lw_lines"], 5)
        self.assertEqual(b["nonlw_lines"], 1)
        self.assertEqual(b["bad_bytes"], 1)
        self.assertEqual(len(b["gaps"]), 1, b["gaps"])  # the 3.6 s one
        self.assertGreater(b["gaps"][0]["gap_s"], 3.0)
        self.assertEqual(res["anomalies"], len(b["anomalies"]))
        self.assertGreater(res["anomalies"], 0)


if __name__ == "__main__":
    unittest.main()
