"""Lane C stage C3 guards: `python3 -m agent.fix` end to end in a subprocess under an audit hook.

Checks that the whole command opens no file outside the bench repo, its temp
clone and Lab Watch's own config, state and code (never ~/fw, which exists in
the fake home with a sentinel), that every git it runs works on the bench
repo or the temp clone, and that no token is in any argv or in the output.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.test_c_fake_forgejo import BENCH_TOKEN, SETUP_TOKEN
from tests.test_c_fix import ENDPOINT_TOKEN, GOOD_DIFF, IID, REPO_ROOT, World, reply

RUNNER = r'''
import json, os, runpy, sys
log, on = [], [True]
def hook(ev, args):
    if not on[0]:
        return
    if ev == "open" and isinstance(args[0], (str, bytes)):
        p = os.fsdecode(args[0])
        log.append(["open", os.path.abspath(p)])
    elif ev == "subprocess.Popen":
        log.append(["popen", [os.fsdecode(a) for a in args[1]]])
    elif ev in ("os.listdir", "os.scandir") and isinstance(args[0], (str, bytes)):
        log.append(["list", os.path.abspath(os.fsdecode(args[0]))])
sys.addaudithook(hook)
out = sys.argv[1]
sys.argv = ["agent.fix"] + sys.argv[2:]
rc = 0
try:
    runpy.run_module("agent.fix", run_name="__main__", alter_sys=True)
except SystemExit as e:
    rc = e.code
finally:
    on[0] = False
    with open(out, "w") as f:
        json.dump({"rc": rc, "log": log}, f)
'''


class GuardTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._td.name)
        self.w = World(self.tmp)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "fw", "src"))
        with open(os.path.join(self.home, "fw", "src", "demo.c"), "w") as f:
            f.write("/* sentinel: never read */\n")
        self.tmpdir = os.path.join(self.tmp, "tmpdir")
        os.makedirs(self.tmpdir)
        self.runner = os.path.join(self.tmp, "runner.py")
        with open(self.runner, "w") as f:
            f.write(RUNNER)

    def tearDown(self):
        self.w.close()
        self._td.cleanup()

    def run_cmd(self, *args):
        trace = os.path.join(self.tmp, "trace.json")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home,
               "TMPDIR": self.tmpdir, "LW_FW_REPO": self.w.bare, "LW_SECRETS_DIR": self.w.secrets,
               "LW_TRACKER": self.w.fake.base, "LW_BENCH_URL": self.w.endpoint.base,
               "LW_DB": self.w.db_path, "LW_TRACKER_STATE": self.w.state_path,
               "LW_HOLD": self.w.hold, "LW_AGENT_CONFIG": self.w.config,
               "PYTHONPATH": os.pathsep.join([REPO_ROOT] + [p for p in os.environ.get(
                   "PYTHONPATH", "").split(os.pathsep) if p])}
        p = subprocess.run([sys.executable, self.runner, trace] + list(args), cwd=REPO_ROOT,
                           env=env, capture_output=True, timeout=90)
        with open(trace) as f:
            t = json.load(f)
        return t["rc"], t["log"], (p.stdout + p.stderr).decode("utf-8", "replace")

    def allowed_roots(self):
        roots = {REPO_ROOT, self.w.bare, self.tmpdir, self.w.secrets, self.w.config,
                 self.w.db_path, self.w.state_path, self.w.hold, "/dev/null"}
        roots |= {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix,
                                                sys.base_exec_prefix)}
        roots |= {os.path.realpath(p) for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                  if p}
        return roots

    def check_trace(self, log, output):
        roots = self.allowed_roots()
        fw = os.path.join(self.home, "fw")
        for kind, *rest in log:
            if kind in ("open", "list"):
                path = os.path.realpath(rest[0])
                self.assertFalse(path == fw or path.startswith(fw + os.sep), path)
                ok = any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) or
                         path.startswith(r + "-") for r in roots)  # db -wal / -shm
                ok = ok or path in (os.path.dirname(self.w.db_path),
                                    os.path.dirname(self.w.state_path))  # SQLite's dir fsync
                self.assertTrue(ok, "opened outside the allowed paths: %s" % path)
            else:
                argv = rest[0]
                blob = " ".join(argv)
                for tok in (SETUP_TOKEN, BENCH_TOKEN, ENDPOINT_TOKEN):
                    self.assertNotIn(tok, blob)
                self.assertNotIn(fw, blob)
                if os.path.basename(argv[0]) == "git":
                    self.assertEqual(argv[1], "-C", argv)
                    target = os.path.realpath(argv[2])
                    self.assertTrue(target in (self.w.bare, self.tmpdir) or
                                    target.startswith(self.tmpdir + os.sep), argv)
        for tok in (SETUP_TOKEN, BENCH_TOKEN, ENDPOINT_TOKEN):
            self.assertNotIn(tok, output)

    def test_good_fix_end_to_end_stays_inside(self):
        self.w.set_reply(reply(GOOD_DIFF))
        rc, log, output = self.run_cmd("--incident", str(IID))
        self.assertEqual(rc, 0, output)
        self.assertIn("PR #1 ", output)
        self.assertEqual(len(self.w.fake.pulls()), 1)
        self.assertTrue(any(k == "popen" and "push" in r[0] for k, *r in log))
        self.check_trace(log, output)
        self.assertEqual(os.listdir(self.tmpdir), [])  # the temp clone is gone
        # The trace is not vacuous: it saw the token file opened and git archive run.
        opened = [r[0] for k, *r in log if k == "open"]
        self.assertIn(os.path.join(self.w.secrets, "tracker_setup_token"), opened)
        self.assertTrue(any(k == "popen" and "archive" in r[0] for k, *r in log))

    def test_checker_catches_a_read_of_the_full_clone(self):
        bad = [["open", os.path.join(self.home, "fw", "src", "demo.c")]]
        with self.assertRaises(AssertionError):
            self.check_trace(bad, "")
        with self.assertRaises(AssertionError):
            self.check_trace([["popen", ["git", "-C", os.path.join(self.home, "fw"), "log"]]], "")
        with self.assertRaises(AssertionError):
            self.check_trace([["popen", ["git", "-c", "x=" + SETUP_TOKEN]]], "")

    def test_failed_and_rejected_runs_stay_inside(self):
        for text in (reply(GOOD_DIFF.replace("+    for (int i = 0; i < SLOTS; i++) {",
                                             "+    for (int i = 0; i <= SLOTS - 0; i++) {")),
                     reply(GOOD_DIFF.replace("src/demo.c", "src/../../fw/src/demo.c")),
                     "garbage"):
            with self.subTest(text[:40]):
                self.w.set_reply(text)
                rc, log, output = self.run_cmd("--incident", str(IID))
                self.assertIn(rc, (3, 4), output)
                self.check_trace(log, output)
        self.assertEqual(self.w.fake.pulls(), [])

    def test_probe_scope_says_setup_token(self):
        self.w.fake._new_repo("bench-fw")
        rc, log, output = self.run_cmd("--probe-scope")
        self.assertEqual(rc, 0, output)
        self.assertIn("refused", output)
        self.check_trace(log, output)


if __name__ == "__main__":
    unittest.main()
