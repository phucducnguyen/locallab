"""Lane A: bench repo check, build, NUCLEO drive, copy, and flash-verify-capture.

No board, no GB10, no network. Git repos are made in temp dirs with a fake
Makefile; the drive is a temp dir; the board is a pty that "boots" when
fw.bin appears and replays a recording from ../fw-public/logs with its
BOOT fw= rewritten to the sha it was flashed with. Recordings are picked by
what their LW lines show (protocol only), not by name, except main.raw.
The real firmware build runs only when arm-none-eabi-gcc is on PATH.
"""

import contextlib
import glob
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from bench import check_bench_repo, flash, parser, store
from bench.incidents import Machine
from bench.run import Bench, Loop

try:
    import serial  # noqa: F401
    HAVE_SERIAL = True
except ImportError:
    HAVE_SERIAL = False

HERE = os.path.dirname(os.path.abspath(__file__))
FW_PUBLIC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "fw-public")
LOGS = os.path.join(FW_PUBLIC, "logs")
GIT = ["git", "-c", "user.name=Lab Watch test", "-c", "user.email=lw@example.invalid",
       "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]

MAKEFILE = (
    "all:\n"
    "\t@if [ -f broken ]; then echo 'src/x.c:1:1: error: made-up failure' >&2; exit 2; fi\n"
    "\tmkdir -p build\n"
    "\tprintf 'BIN %s' '$(FW_VERSION)' > build/fw.bin\n"
    "\tprintf 'ELF %s' '$(FW_VERSION)' > build/fw.elf\n"
)


def git(repo, *args):
    p = subprocess.run(GIT + ["-C", repo] + list(args), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, check=True)
    return p.stdout.decode().strip()


def make_repo(path, files=None):
    os.makedirs(path)
    git(path, "init", "-q")
    for name, text in dict({"Makefile": MAKEFILE}, **(files or {})).items():
        full = os.path.join(path, name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "first")
    return git(path, "rev-parse", "HEAD")


def commit(repo, msg, files=None, remove=()):
    for name, text in (files or {}).items():
        with open(os.path.join(repo, name), "w") as f:
            f.write(text)
    for name in remove:
        git(repo, "rm", "-rq", name)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


class TestCheckBenchRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "bench-fw")

    def test_main_only_after_removal_commit_passes(self):
        make_repo(self.repo, {"CASES.md": "x\n", "logs/a.raw": "x\n", "bugs/a.patch": "x\n",
                              "src/a.c": "int a;\n"})
        self.assertEqual(len(check_bench_repo.check(self.repo)), 1)
        commit(self.repo, "bench: remove answer key", remove=("CASES.md", "logs", "bugs"))
        self.assertEqual(check_bench_repo.check(self.repo), [])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(check_bench_repo.main(["--repo", self.repo]), 0)
        self.assertIn("bench repo OK", out.getvalue())

    def test_each_forbidden_path_refused(self):
        make_repo(self.repo, {"src/a.c": "int a;\n"})
        for name in ("CASES.md", "logs", "bugs"):
            with self.subTest(path=name):
                full = os.path.join(self.repo, name)
                if name == "CASES.md":
                    files = {name: "x\n"}
                else:
                    os.makedirs(full, exist_ok=True)
                    files = {os.path.join(name, "f"): "x\n"}
                commit(self.repo, "add", files)
                probs = check_bench_repo.check(self.repo)
                self.assertTrue(any(name in p for p in probs), probs)
                commit(self.repo, "drop", remove=(name,))
                self.assertEqual(check_bench_repo.check(self.repo), [])

    def test_extra_refs_refused(self):
        make_repo(self.repo)
        for kind, args in (("branch", ["branch", "other"]), ("tag", ["tag", "v1"]),
                           ("remote", ["update-ref", "refs/remotes/origin/main", "HEAD"])):
            with self.subTest(ref=kind):
                git(self.repo, *args)
                probs = check_bench_repo.check(self.repo)
                self.assertTrue(any("besides main" in p for p in probs), probs)
                self.assertEqual(check_bench_repo.main(["--repo", self.repo]), 1)
                ref = {"branch": "refs/heads/other", "tag": "refs/tags/v1",
                       "remote": "refs/remotes/origin/main"}[kind]
                git(self.repo, "update-ref", "-d", ref)
        self.assertEqual(check_bench_repo.check(self.repo), [])

    def test_missing_or_not_a_repo(self):
        self.assertTrue(check_bench_repo.check(os.path.join(self.tmp.name, "nope")))
        os.makedirs(os.path.join(self.tmp.name, "plain"))
        self.assertTrue(check_bench_repo.check(os.path.join(self.tmp.name, "plain")))

    def test_bench_run_refuses_to_start(self):
        from bench import run
        make_repo(self.repo, {"CASES.md": "x\n"})
        sha = git(self.repo, "rev-parse", "--short=7", "HEAD")
        rc = run.main(["--flash", sha, "--repo", self.repo,
                       "--db", os.path.join(self.tmp.name, "b.db")])
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "b.db")))


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "bench-fw")
        self.state = os.path.join(self.tmp.name, "state")
        self.full = make_repo(self.repo, {"src/a.c": "int a;\n"})

    def test_build_exports_and_keeps_elf(self):
        b = flash.build(self.repo, self.full, self.state)
        short = self.full[:7]
        self.assertTrue(b["ok"], b)
        self.assertEqual((b["sha"], b["full"], b["subject"]), (short, self.full, "first"))
        with open(b["bin"]) as f:
            self.assertEqual(f.read(), "BIN %s" % short)  # make FW_VERSION=<short>
        self.assertEqual(b["elf"], os.path.join(self.state, "elf", "%s.elf" % short))
        with open(b["elf"]) as f:
            self.assertEqual(f.read(), "ELF %s" % short)
        # the build dir is an export, not the repo; a rebuild starts clean
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(os.path.dirname(b["bin"])),
                                                     ".git")))
        junk = os.path.join(self.state, "build", short, "junk")
        open(junk, "w").close()
        b2 = flash.build(self.repo, short, self.state)
        self.assertTrue(b2["ok"])
        self.assertFalse(os.path.exists(junk))

    def test_toolchain_passed_to_make(self):
        b = flash.build(self.repo, self.full, self.state, toolchain="/opt/x/arm-none-eabi-")
        self.assertIn("TOOLCHAIN=/opt/x/arm-none-eabi-", b["cmd"])
        self.assertIn("FW_VERSION=%s" % self.full[:7], b["cmd"])

    def test_refuses_bad_shas(self):
        git(self.repo, "checkout", "-qb", "side")
        side = commit(self.repo, "side work")
        git(self.repo, "checkout", "-q", "main")
        for bad in ("main", "--output=x", "abc", side[:7], "f" * 40, self.full[:7] + "\n"):
            with self.subTest(sha=bad):
                b = flash.build(self.repo, bad, self.state)
                self.assertFalse(b["ok"])
                self.assertTrue(b.get("bench_fault"))

    def test_build_error(self):
        sha = commit(self.repo, "break it", {"broken": "1"})
        b = flash.build(self.repo, sha, self.state)
        self.assertFalse(b["ok"])
        self.assertEqual(b["why"], "src/x.c:1:1: error: made-up failure")
        # BUILD_ERROR run: CODE when the toolchain is there, UNKNOWN when it is not
        tc_dir = os.path.join(self.tmp.name, "tc")
        os.makedirs(tc_dir)
        gcc = os.path.join(tc_dir, "arm-none-eabi-gcc")
        with open(gcc, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(gcc, 0o755)
        for tc, triage in ((tc_dir + "/arm-none-eabi-", "CODE"),
                           (os.path.join(self.tmp.name, "none", "x-"), "UNKNOWN")):
            with self.subTest(triage=triage):
                b = flash.build(self.repo, sha, self.state, toolchain=tc)
                conn = store.open_db(os.path.join(self.tmp.name, "%s.db" % triage))
                bench = Bench(conn, "nucleo-1", log=lambda s: None)
                bench.build_failed(b, t=5.0)
                runs = [dict(r) for r in conn.execute("SELECT * FROM runs")]
                self.assertEqual([(r["verdict"], r["triage"], r["commit"]) for r in runs],
                                 [("BUILD_ERROR", triage, None)])
                routing = [dict(r) for r in conn.execute("SELECT * FROM routing")]
                self.assertEqual(len(routing), 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM pass_history").fetchone()[0],
                                 0)
                conn.close()


class FakeRun:
    """Stands in for udisksctl / mount / umount; never sudo."""

    def __init__(self, mounts_file, ok=(), on_ok=None):
        self.cmds = []
        self.ok = set(ok)
        self.on_ok = on_ok or {}
        self.mounts_file = mounts_file

    def __call__(self, cmd):
        self.cmds.append(cmd)
        if cmd[0] in self.ok:
            line = self.on_ok.get(cmd[0])
            if line:
                with open(self.mounts_file, "a") as f:
                    f.write(line)
            return 0, ""
        return 1, "Not authorized to perform operation"


class TestDrive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.by_label = os.path.join(d, "by-label")
        os.makedirs(self.by_label)
        self.dev = os.path.join(d, "sdc")
        open(self.dev, "w").close()
        os.symlink(self.dev, os.path.join(self.by_label, "NOD_F446RE"))
        self.base = os.path.join(d, "media", "dell")
        self.mp = os.path.join(self.base, "NOD_F446RE")
        os.makedirs(self.mp)
        self.mounts = os.path.join(d, "mounts")
        self.fstab = os.path.join(d, "fstab")
        open(self.fstab, "w").close()

    def drive(self, run, mounts=""):
        with open(self.mounts, "w") as f:
            f.write("proc /proc proc rw 0 0\n" + mounts)
        return flash.Drive(user="dell", bases=[os.path.join(self.tmp.name, "run", "dell"),
                                               self.base],
                           by_label=self.by_label, proc_mounts=self.mounts, fstab=self.fstab,
                           run=run, fixed="", device_wait_s=0)

    def test_live_mount_used_without_commands(self):
        run = FakeRun(self.mounts)
        d = self.drive(run, "%s %s vfat rw,uid=1000 0 0\n" % (self.dev, self.mp))
        self.assertEqual(d.locate(), (self.mp, None))
        self.assertEqual(run.cmds, [])

    def test_stale_mount_then_udisks_refused_then_fstab_mount(self):
        stale = "/dev/sdb %s vfat rw 0 0\n" % self.mp   # the board before re-enumerating
        run = FakeRun(self.mounts, ok={"umount", "mount"},
                      on_ok={"mount": "%s %s vfat rw 0 0\n" % (self.dev, self.mp)})
        d = self.drive(run, stale)
        self.assertEqual(d.locate(), (self.mp, None))
        self.assertEqual([c[0] for c in run.cmds], ["udisksctl", "umount", "mount"])
        self.assertEqual(run.cmds[0][-2:], ["-b", os.path.join(self.by_label, "NOD_F446RE")])
        self.assertEqual(run.cmds[2], ["mount", self.mp])

    def test_udisks_works(self):
        run = FakeRun(self.mounts, ok={"udisksctl"},
                      on_ok={"udisksctl": "%s %s vfat rw 0 0\n" % (self.dev, self.mp)})
        self.assertEqual(self.drive(run).locate(), (self.mp, None))
        self.assertEqual(len(run.cmds), 1)

    def test_read_only_mount_not_used(self):
        run = FakeRun(self.mounts)
        mp, why = self.drive(run, "%s %s vfat ro 0 0\n" % (self.dev, self.mp)).locate()
        self.assertIsNone(mp)

    def test_everything_refused_names_the_command(self):
        run = FakeRun(self.mounts)
        mp, why = self.drive(run).locate()
        self.assertIsNone(mp)
        self.assertIn("sudo mount -o uid=", why)
        self.assertIn("user,noauto", why)
        self.assertIn(self.mp, why)
        self.assertFalse(any(c[0] == "sudo" for c in run.cmds))

    def test_no_device(self):
        os.unlink(os.path.join(self.by_label, "NOD_F446RE"))
        mp, why = self.drive(FakeRun(self.mounts)).locate()
        self.assertIsNone(mp)
        self.assertIn("no NUCLEO drive", why)

    def test_full_label_and_fixed_dir(self):
        os.symlink(self.dev, os.path.join(self.by_label, "NODE_F446RE"))
        os.unlink(os.path.join(self.by_label, "NOD_F446RE"))
        mp2 = os.path.join(self.tmp.name, "run", "dell", "NODE_F446RE")
        os.makedirs(mp2)
        run = FakeRun(self.mounts)
        self.assertEqual(self.drive(run, "%s %s vfat rw 0 0\n" % (self.dev, mp2)).locate(),
                         (mp2, None))
        d = flash.Drive(fixed=self.mp)
        self.assertEqual(d.locate(), (self.mp, None))
        self.assertIsNone(flash.Drive(fixed=os.path.join(self.tmp.name, "nope")).locate()[0])


class Breaking:
    """A file object that fails after n bytes (USB pulled mid-copy)."""

    def __init__(self, n):
        self.n = n
        self.written = 0

    def __call__(self, path, mode):
        return self

    def write(self, b):
        if self.written + len(b) > self.n:
            raise OSError(5, "Input/output error")
        self.written += len(b)
        return len(b)

    def flush(self):
        pass

    def fileno(self):
        raise ValueError("no fd")

    def close(self):
        pass


class TestCopy(unittest.TestCase):
    def test_copy_and_mid_copy_failure(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "fw.bin")
            with open(src, "wb") as f:
                f.write(os.urandom(50000))
            drive = os.path.join(d, "drive")
            os.makedirs(drive)
            ok, msg = flash.copy_bin(src, drive, sync=lambda: None)
            self.assertTrue(ok, msg)
            with open(src, "rb") as a, open(os.path.join(drive, "fw.bin"), "rb") as b:
                self.assertEqual(a.read(), b.read())
            ok, msg = flash.copy_bin(src, drive, opener=Breaking(20000), chunk=4096,
                                     sync=lambda: None)
            self.assertFalse(ok)
            self.assertIn("copy failed after 16384 of 50000 bytes", msg)
            ok, msg = flash.copy_bin(src, os.path.join(d, "gone"), sync=lambda: None)
            self.assertFalse(ok)


# ---- flash, verify, capture with a fake board on a pty ----

def recordings():
    out = []
    for path in sorted(glob.glob(os.path.join(LOGS, "*.raw"))):
        with open(path, "rb") as f:
            data = f.read()
        recs = [r for ln in data.split(b"\n") for r in parser.parse_line(ln.rstrip(b"\r"))[
            "records"]]
        out.append((path, data, recs))
    return out


def pick(pred):
    for path, data, recs in recordings():
        if pred(data, recs):
            return path
    return None


def kinds(recs, k):
    return [r for r in recs if r["kind"] == k]


GOOD = os.path.join(LOGS, "main.raw")
FAULT = pick(lambda d, r: len(kinds(r, "BOOT")) == 1 and kinds(r, "HARDFAULT")
             and not kinds(r, "RESULT"))
LOOP = pick(lambda d, r: len(kinds(r, "BOOT")) >= 3 and not kinds(r, "HARDFAULT")
            and all("IWDG" in b["reset"] for b in kinds(r, "BOOT")[1:]))
NOISE = pick(lambda d, r: not r and parser.count_bad_bytes(d) >= 8)
BOOT_RE = re.compile(rb"(LW BOOT fw=)\S+")


class FakeBoard(threading.Thread):
    """Prints its current build's last line every 0.3 s; when fw.bin appears on
    the drive it takes the sha written in it, keeps printing the old build's
    line briefly (stale bytes), then plays the next recording of its plan with
    BOOT fw= rewritten to that sha. A plan item of None prints nothing."""

    def __init__(self, port, drive, plan, rewrite=None):
        super().__init__(daemon=True)
        self.port = port
        self.drive = drive
        self.plan = list(plan)
        self.rewrite = rewrite
        self.stop = threading.Event()
        self.last = None
        self.boots = []

    def _say(self, data):
        try:
            self.port.write_paced(data, chunk=64)
        except OSError:
            pass

    def run(self):
        binp = os.path.join(self.drive, "fw.bin")
        next_repeat = 0
        while not self.stop.is_set():
            if os.path.exists(binp):
                time.sleep(0.05)
                with open(binp, "rb") as f:
                    content = f.read()
                os.unlink(binp)  # the drive re-enumerates empty
                sha = content.split()[-1] if content.startswith(b"BIN ") else b"0000000"
                self.boots.append(sha.decode())
                if self.last:
                    for _ in range(3):
                        self._say(self.last)
                        time.sleep(0.05)
                item = self.plan.pop(0) if self.plan else None
                self.last = None
                if item is not None:
                    with open(item, "rb") as f:
                        data = f.read()
                    data = BOOT_RE.sub(lambda m: m.group(1) + (self.rewrite or sha), data)
                    self._say(data)
                    lw = [ln for ln in data.split(b"\n") if ln.startswith(b"LW ")]
                    self.last = (lw[-1] + b"\n") if lw else None
                next_repeat = time.monotonic() + 0.3
            elif self.last and time.monotonic() >= next_repeat:
                self._say(self.last)
                next_repeat = time.monotonic() + 0.3
            time.sleep(0.02)


@unittest.skipUnless(HAVE_SERIAL, "pyserial not importable")
@unittest.skipUnless(os.path.isfile(GOOD), "no recordings")
class TestFlashVerifyCapture(unittest.TestCase):
    def setUp(self):
        from bench.serial_owner import SerialOwner
        from tests.test_a_serial_owner import FakePort
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.repo = os.path.join(d, "bench-fw")
        self.good = make_repo(self.repo, {"src/a.c": "int a;\n"})[:7]
        self.second = commit(self.repo, "second change", {"src/a.c": "int a = 2;\n"})[:7]
        self.state = os.path.join(d, "state")
        self.drive = os.path.join(d, "NOD_F446RE")
        os.makedirs(self.drive)
        link = os.path.join(d, "usb-STMicroelectronics_STLINK-V2-1_TEST-if02")
        self.port = FakePort(link)
        self.addCleanup(self.port.unplug)
        self.owner = SerialOwner(port_glob=os.path.join(d, "*STM*"), idle_flush=0.2,
                                 reopen_interval=0.1).start()
        self.addCleanup(self.owner.stop)
        self.conn = store.open_db(os.path.join(d, "bench.db"))
        self.addCleanup(self.conn.close)
        self.logs = []
        self.bench = Bench(self.conn, "nucleo-1", log=self.logs.append, fault_tail_s=0.5,
                           machine=Machine("nucleo-1", capture_s=6.0))
        self.probed = []
        self.bench.probe = self.probed.append
        self.loop = Loop(self.bench, self.owner.events, self.conn)

    def board(self, plan, rewrite=None):
        b = FakeBoard(self.port, self.drive, plan, rewrite)
        b.start()
        self.addCleanup(b.stop.set)
        return b

    def flash(self, sha, opener=open, timeout=20.0):
        n = len(self.bench.finished)
        job = flash.FlashJob(self.bench, self.loop.call, self.repo, sha, self.state,
                             drive=flash.Drive(fixed=self.drive), window=2.0, opener=opener,
                             sync=lambda: None, log=self.logs.append).start()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.loop.step(0.05)
            if job.done.is_set() and len(self.bench.finished) > n:
                return job
        self.fail("flash of %s did not finish: %s" % (sha, self.logs[-5:]))

    def rows(self, sql, *a):
        return [dict(r) for r in self.conn.execute(sql, a).fetchall()]

    def settle(self, s=0.3):
        end = time.monotonic() + s
        while time.monotonic() < end:
            self.loop.step(0.05)

    def test_good_flash_passes_and_stale_bytes_are_discarded(self):
        self.board([GOOD])
        self.settle()
        self.flash(self.good)
        runs = self.rows("SELECT * FROM runs")
        self.assertEqual([(r["commit"], r["verdict"], r["triage"], r["subject"]) for r in runs],
                         [(self.good, "PASS", "NONE", "first")])
        ev = json.loads(runs[0]["evidence"])
        self.assertTrue(ev["verified"])
        self.assertEqual(ev["elf"], os.path.join(self.state, "elf", "%s.elf" % self.good))
        self.assertEqual([r["sha"] for r in self.rows("SELECT * FROM pass_history")],
                         [self.good])
        self.assertEqual([(f["how"], f["result"]) for f in self.rows("SELECT * FROM flashes")],
                         [("usb", "VERIFIED")])
        self.assertEqual(self.rows("SELECT * FROM incidents"), [])

    def test_hardfault_build_then_harmless_control(self):
        self.assertIsNotNone(FAULT, "no recording with one BOOT and a HARDFAULT")
        self.board([FAULT, GOOD])
        self.settle()
        self.flash(self.second)
        self.settle(0.8)   # the old build keeps repeating its HARDFAULT line
        self.flash(self.good)
        runs = self.rows("SELECT * FROM runs ORDER BY run_id")
        self.assertEqual([(r["commit"], r["verdict"], r["triage"]) for r in runs],
                         [(self.second, "HARDFAULT", "CODE"), (self.good, "PASS", "NONE")])
        ev = json.loads(runs[0]["evidence"])
        self.assertIn("after_fault", ev)
        self.assertGreater(json.loads(runs[1]["evidence"])["discarded"], 0)  # stale bytes
        inc = self.rows("SELECT * FROM incidents")
        self.assertEqual([(i["kind"], i["triage"], i["state"], i["firmware"]) for i in inc],
                         [("HARDFAULT", "CODE", "RESOLVED", self.second)])
        self.assertEqual([r["sha"] for r in self.rows("SELECT * FROM pass_history")],
                         [self.good])
        stale = [e for e in self.rows("SELECT * FROM events WHERE kind='HARDFAULT'")
                 if e["t_wall"] > runs[1]["t_start"]]
        self.assertEqual(stale, [])

    def test_boot_of_another_sha_is_flash_error(self):
        self.board([GOOD], rewrite=b"0123abc")
        self.flash(self.good)
        runs = self.rows("SELECT * FROM runs")
        self.assertEqual([(r["commit"], r["verdict"], r["triage"]) for r in runs],
                         [(None, "FLASH_ERROR", "UNKNOWN")])
        self.assertEqual(self.rows("SELECT * FROM pass_history"), [])
        self.assertEqual(self.rows("SELECT route FROM routing"), [{"route": "Triage"}])

    def test_silent_board_is_no_output(self):
        self.board([None])
        self.flash(self.good)
        runs = self.rows("SELECT * FROM runs")
        self.assertEqual([(r["commit"], r["verdict"], r["triage"]) for r in runs],
                         [(None, "NO_OUTPUT", "UNKNOWN")])
        inc = self.rows("SELECT * FROM incidents")
        self.assertEqual([(i["kind"], i["route"], i["firmware"]) for i in inc],
                         [("RUN_FAIL", "Triage", None)])
        self.assertEqual(self.rows("SELECT * FROM pass_history"), [])

    def test_unreadable_bytes_are_garbled(self):
        self.assertIsNotNone(NOISE, "no recording of unreadable bytes")
        self.board([NOISE])
        self.flash(self.good)
        runs = self.rows("SELECT * FROM runs")
        self.assertEqual([(r["verdict"], r["triage"]) for r in runs], [("GARBLED", "UNKNOWN")])
        self.assertEqual(self.probed, [runs[0]["run_id"]])
        self.bench.attach_probe(runs[0]["run_id"], {"decodes_at": None, "tried": []})
        ev = json.loads(self.rows("SELECT evidence FROM runs")[0]["evidence"])
        self.assertEqual(ev["rate_probe"]["decodes_at"], None)

    def test_watchdog_boots_are_reset_loop(self):
        self.assertIsNotNone(LOOP, "no recording of repeated IWDG boots")
        self.board([LOOP])
        self.flash(self.good)
        runs = self.rows("SELECT * FROM runs")
        self.assertEqual([(r["commit"], r["verdict"], r["triage"]) for r in runs],
                         [(self.good, "RESET_LOOP", "UNKNOWN")])
        self.assertEqual(self.rows("SELECT * FROM pass_history"), [])

    def test_usb_pulled_mid_copy_is_flash_error(self):
        self.board([GOOD, GOOD])
        self.flash(self.good)
        self.flash(self.second, opener=Breaking(4))
        runs = self.rows("SELECT * FROM runs ORDER BY run_id")
        self.assertEqual([(r["verdict"], r["triage"]) for r in runs],
                         [("PASS", "NONE"), ("FLASH_ERROR", "UNKNOWN")])
        self.assertIn("copy failed", json.loads(runs[1]["evidence"])["why"])
        self.assertEqual([r["sha"] for r in self.rows("SELECT * FROM pass_history")],
                         [self.good])
        self.assertEqual(self.rows("SELECT result FROM flashes ORDER BY id")[-1]["result"],
                         "FLASH_ERROR")
        self.assertIsNone(self.bench.m.verified_sha)


class FakeSerialModule:
    """Serial() whose bytes read cleanly only at one rate."""

    def __init__(self, good_rate):
        self.good_rate = good_rate
        self.opened = []

    def Serial(self, port, baudrate, **kw):
        self.opened.append(baudrate)
        mod = self

        class S:
            in_waiting = 0

            def __init__(self):
                self.sent = False

            def read(self, n):
                if self.sent:
                    time.sleep(0.01)
                    return b""
                self.sent = True
                if baudrate == mod.good_rate:
                    return b"LW ALIVE 3\r\nLW HEALTH 3 loopback=OK\r\n"
                return bytes(range(0x80, 0xc0))

            def close(self):
                pass
        return S()


class TestRateProbe(unittest.TestCase):
    def test_probe_names_the_rate_that_decodes(self):
        from bench.serial_owner import SerialOwner
        for good, want in ((57600, 57600), (1, None)):
            with self.subTest(good=good):
                mod = FakeSerialModule(good)
                owner = SerialOwner(port="/dev/null-test", serial_module=mod)
                res = owner.probe_rates("/dev/null-test", (9600, 57600, 230400), 0.05)
                self.assertEqual(mod.opened, [9600, 57600, 230400])
                self.assertEqual(res["decodes_at"], want)
                self.assertEqual([t["rate"] for t in res["tried"]], [9600, 57600, 230400])


class TestCaptureMachine(unittest.TestCase):
    """Capture rules without a pty: the 15 s cap and a non-IWDG boot in a run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.open_db(os.path.join(self.tmp.name, "b.db"))
        self.addCleanup(self.conn.close)
        self.bench = Bench(self.conn, "nucleo-1", log=lambda s: None)
        self.bench.apply(self.bench.m.link_up(0.0), 0.0)

    def line(self, t, text):
        self.bench.line(t, "T", parser.parse_line(text)["records"])
        self.bench.tick(t)

    def test_no_result_within_capture_is_no_output(self):
        self.bench.begin_flash("abcdef1", "s", None, t=1.0)
        self.bench.flash_copied("ok", t=2.0)
        self.line(3.0, "LW BOOT fw=abcdef1 reset=PIN csr=0x04000000")
        self.line(4.0, "LW TEST crc32 PASS")
        self.bench.tick(17.9)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.bench.tick(18.1)
        r = dict(self.conn.execute("SELECT * FROM runs").fetchone())
        self.assertEqual((r["commit"], r["verdict"], r["triage"]), ("abcdef1", "NO_OUTPUT",
                                                                    "UNKNOWN"))

    def test_link_drop_during_flash_opens_nothing(self):
        self.bench.begin_flash("abcdef1", "s", None, t=1.0)
        self.bench.apply(self.bench.m.link_down(1.5), 1.5)
        self.bench.tick(14.0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)
        self.bench.apply(self.bench.m.link_up(14.5), 14.5)
        self.bench.flash_copied("ok", t=14.6)
        self.line(15.0, "LW BOOT fw=abcdef1 reset=PIN csr=0x04000000")
        self.line(15.1, "LW RESULT PASS")
        self.assertEqual(self.conn.execute("SELECT verdict FROM runs").fetchone()[0], "PASS")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)

    def test_reset_button_during_run_is_not_a_reset_loop(self):
        self.bench.begin_flash("abcdef1", "s", None, t=1.0)
        self.bench.flash_copied("ok", t=2.0)
        self.line(3.0, "LW BOOT fw=abcdef1 reset=PIN csr=0x04000000")
        self.line(3.5, "LW BOOT fw=abcdef1 reset=PIN csr=0x04000000")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        kinds_ = [r[0] for r in self.conn.execute("SELECT kind FROM incidents")]
        self.assertEqual(kinds_, ["REBOOT"])


@unittest.skipUnless(shutil.which("arm-none-eabi-gcc"), "arm-none-eabi-gcc not on PATH")
@unittest.skipUnless(os.path.isdir(os.path.join(FW_PUBLIC, ".git")), "no ../fw-public clone")
class TestRealFirmwareBuild(unittest.TestCase):
    def test_build_main(self):
        """git archive of ../fw-public main into a temp dir, then make (sources unread)."""
        with tempfile.TemporaryDirectory() as d:
            main = git(FW_PUBLIC, "rev-parse", "refs/heads/main")
            b = flash.build(FW_PUBLIC, main, d)
            self.assertTrue(b["ok"], b)
            self.assertGreater(os.path.getsize(b["bin"]), 0)
            self.assertTrue(os.path.isfile(os.path.join(d, "elf", "%s.elf" % main[:7])))


if __name__ == "__main__":
    unittest.main()
