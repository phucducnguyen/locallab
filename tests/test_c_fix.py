"""Lane C stage C3: the fix beat (tracker/fix.py), in process, against temp repos and fakes.

No network beyond 127.0.0.1, no board, no GB10. The bench repo is a temp bare
repo holding an invented C file; the agent is a script printing a canned
reply; the flash step is a fake that "passes" only when the fixed line is in
the tree it is given; the tracker is tests/test_c_fake_forgejo.py.
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tests.test_c_fake_forgejo import BENCH_TOKEN, SETUP_TOKEN, FakeForgejo
from tracker import fix
from tracker.forgejo import Forgejo, TrackerError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENDPOINT_TOKEN = "fake-endpoint-token"
IID = 3

GOOD_C = """\
/* demo.c: invented example code for the Lab Watch tests. */
#include <stdint.h>

#define SLOTS 8

static uint32_t slots[SLOTS];

void demo_fill(uint32_t v)
{
    for (int i = 0; i < SLOTS; i++) {
        slots[i] = v;
    }
}

uint32_t demo_sum(void)
{
    uint32_t s = 0;
    for (int i = 0; i < SLOTS; i++) {
        s += slots[i];
    }
    return s;
}
"""
BAD_C = GOOD_C.replace("void demo_fill(uint32_t v)\n{\n    for (int i = 0; i < SLOTS; i++)",
                       "void demo_fill(uint32_t v)\n{\n    for (int i = 0; i <= SLOTS; i++)")
BAD_LINE = 10  # the line of "i <= SLOTS" in BAD_C
FIXED_MARK = "for (int i = 0; i < SLOTS; i++) {\n        slots[i] = v;"

GOOD_DIFF = """\
--- a/src/demo.c
+++ b/src/demo.c
@@ -9,3 +9,3 @@ void demo_fill(uint32_t v)
 {
-    for (int i = 0; i <= SLOTS; i++) {
+    for (int i = 0; i < SLOTS; i++) {
         slots[i] = v;
"""


def reply(diff, summary="keep demo_fill inside slots[]"):
    return "Looking at it.\n%s\n%s%s\nSUMMARY: %s\n" % (fix.BEGIN, diff, fix.END, summary)


FAKE_AGENT = r'''
import json, sys
reply_file, record_file = sys.argv[1], sys.argv[2]
with open(record_file, "a") as f:
    f.write(json.dumps({"argv": sys.argv[3:], "stdin": sys.stdin.read()}) + "\n")
sys.stdout.write(open(reply_file).read())
'''

FAKE_FLASH = r'''
import os, sys
src, sha = sys.argv[1], sys.argv[2]
text = open(os.path.join(src, "src", "demo.c")).read()
good = "for (int i = 0; i < SLOTS; i++) {\n        slots[i] = v;" in text
print("noise before boot")
print("LW BOOT fw=%s reset=PIN csr=0x04000000" % sha[:7])
print("LW TEST demo_fill %s" % ("PASS" if good else "FAIL got=9 want=8"))
print("LW RESULT %s" % ("PASS" if good else "FAIL"))
for n in range(1, 4):
    print("LW ALIVE %d" % n)
    print("LW HEALTH %d loopback=OK" % n)
'''


def run(cwd, *args, env=None):
    return subprocess.run(["git", "-C", cwd] + list(args), check=True, capture_output=True,
                          env=env).stdout.decode().strip()


def make_bench_repo(root):
    """A bare repo with main = good commit, then the bad commit. Returns (bare, good, bad)."""
    work = os.path.join(root, "work")
    os.makedirs(os.path.join(work, "src"))
    run(work, "init", "--quiet", "-b", "main")
    ident = ["-c", "user.name=Test", "-c", "user.email=t@example.invalid"]
    with open(os.path.join(work, "src", "demo.c"), "w") as f:
        f.write(GOOD_C)
    with open(os.path.join(work, "README"), "w") as f:
        f.write("invented bench repo\n")
    run(work, "add", ".")
    run(work, *ident, "commit", "--quiet", "-m", "demo: fill and sum")
    good = run(work, "rev-parse", "HEAD")
    with open(os.path.join(work, "src", "demo.c"), "w") as f:
        f.write(BAD_C)
    run(work, *ident, "commit", "--quiet", "-am", "demo: fill one more")
    bad = run(work, "rev-parse", "HEAD")
    bare = os.path.join(root, "bench-fw.git")
    subprocess.run(["git", "clone", "--quiet", "--bare", work, bare], check=True)
    return bare, good, bad


class FakeEndpoint:
    """/incidents/<id> and /show?ref= of the bench endpoint, Bearer auth."""

    def __init__(self, incidents, show):
        self.incidents = incidents
        self.show = show
        self.paths = []
        ep = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                ep.paths.append(self.path)
                code, obj = 404, {"error": "no such route"}
                if self.headers.get("Authorization") != "Bearer " + ENDPOINT_TOKEN:
                    code, obj = 401, {"error": "auth"}
                elif u.path.startswith("/incidents/"):
                    inc = ep.incidents.get(int(u.path.rsplit("/", 1)[1]))
                    code, obj = (200, inc) if inc else (404, {"error": "no such incident"})
                elif u.path == "/show":
                    ref = dict(urllib.parse.parse_qsl(u.query)).get("ref")
                    if ref in ep.show:
                        code, obj = 200, {"ref": ref, "truncated": False,
                                          "data": {"diff": ep.show[ref]}}
                    else:
                        code, obj = 400, {"error": "not a commit on main"}
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def incident(iid, sha, triage="CODE", file_line="src/demo.c:%d" % BAD_LINE):
    return {"id": iid, "board": "nucleo-1", "kind": "HARDFAULT", "state": "OPEN",
            "triage": triage, "firmware": sha[:7], "reason": "This sha's own run faulted",
            "last_pass": {"sha": None, "t": None}, "route": "Firmware team",
            "evidence": {"file_line": file_line,
                         "lines": [{"event_id": 7, "raw_ts": "12:00:01.000",
                                    "line": "LW HARDFAULT pc=0x08000100"}]},
            "routing": []}


def bench_db(path, iids):
    conn = sqlite3.connect(path)
    with open(os.path.join(REPO_ROOT, "docs", "schema.sql")) as f:
        conn.executescript(f.read())
    for iid in iids:
        conn.execute("INSERT INTO incidents (id, board, kind, state, opened, triage, reason) "
                     "VALUES (?, 'nucleo-1', 'HARDFAULT', 'OPEN', 1, 'CODE', 'r')", (iid,))
        conn.execute("INSERT INTO alerts (incident_id, transition, text, created, sent) "
                     "VALUES (?, 'open', 'LAB WATCH incident open', 1, 2)", (iid,))
    conn.commit()
    conn.close()


class World:
    """Everything a fix run needs, in one temp dir."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.bare, self.good, self.bad = make_bench_repo(tmp)
        self.secrets = os.path.join(tmp, "secrets")
        os.makedirs(self.secrets)
        for name, tok in (("tracker_setup_token", SETUP_TOKEN), ("tracker_token", BENCH_TOKEN),
                          ("bench_token", ENDPOINT_TOKEN)):
            with open(os.path.join(self.secrets, name), "w") as f:
                f.write(tok + "\n")
        self.fake = FakeForgejo(search_lag=0.0).start()
        show = subprocess.run(["git", "--git-dir", self.bare, "show", "--format=%H %s",
                               self.bad], check=True, capture_output=True).stdout.decode()
        self.endpoint = FakeEndpoint({IID: incident(IID, self.bad),
                                      4: incident(4, self.bad, triage="HARDWARE"),
                                      5: incident(5, self.bad, file_line="lib/x.c:3")},
                                     {self.bad[:7]: show.splitlines()})
        self.db_path = os.path.join(tmp, "bench.db")
        bench_db(self.db_path, [IID, 4, 5])
        self.state_path = os.path.join(tmp, "tracker_state.db")
        self.hold = os.path.join(tmp, "hold")
        self.reply_file = os.path.join(tmp, "reply.txt")
        self.record = os.path.join(tmp, "agent_calls.jsonl")
        self.agent = os.path.join(tmp, "fake_agent.py")
        with open(self.agent, "w") as f:
            f.write(FAKE_AGENT)
        self.flash = os.path.join(tmp, "fake_flash.py")
        with open(self.flash, "w") as f:
            f.write(FAKE_FLASH)
        self.config = os.path.join(tmp, "agent.json")
        with open(self.config, "w") as f:
            json.dump({"agent_cmd": [sys.executable, self.agent, self.reply_file, self.record,
                                     "--session", "{session}", "--message", "{message}"],
                       "timeout_s": 20, "fix_timeout_s": 20,
                       "verify_cmd": [sys.executable, self.flash, "{src}", "{sha}"]}, f)
        self.flashed = []
        self.flash_mode = "real"

    def set_reply(self, text):
        with open(self.reply_file, "w") as f:
            f.write(text)

    def verifier(self, src, sha):
        """In-process fake flash step: records (sha, fixed?) and answers by the tree."""
        with open(os.path.join(src, "src", "demo.c")) as f:
            good = FIXED_MARK in f.read()
        self.flashed.append((sha, good))
        if self.flash_mode == "error":
            raise fix.VerifyError("flash step exited 1")
        if self.flash_mode == "fail":
            good = False
        lines = ["LW BOOT fw=%s reset=PIN csr=0x04000000" % sha[:7],
                 "LW TEST demo_fill %s" % ("PASS" if good else "FAIL got=9 want=8"),
                 "LW RESULT %s" % ("PASS" if good else "FAIL")]
        for n in range(1, 4):
            lines += ["LW ALIVE %d" % n, "LW HEALTH %d loopback=OK" % n]
        return lines

    def fixer(self, verifier=None, max_lines=fix.DEFAULT_MAX_LINES):
        cfg = fix.load_fix_config(self.config)
        for c in (getattr(self, "db", None), getattr(self, "state", None)):
            if c is not None:
                c.close()
        self.db = fix.connect(self.db_path)
        self.state = fix.connect_fix_state(self.state_path)
        self.logs = []
        return fix.Fixer(fix.Endpoint(self.endpoint.base,
                                      os.path.join(self.secrets, "bench_token")),
                         cfg, fix.check_bench_repo(self.bare), verifier or self.verifier,
                         Forgejo(os.path.join(self.secrets, "tracker_setup_token"),
                                 base=self.fake.base, repo="labwatch/bench-fw"),
                         self.db, self.state, hold_path=self.hold, max_lines=max_lines,
                         log=self.logs.append)

    def agent_calls(self):
        try:
            with open(self.record) as f:
                return [json.loads(ln) for ln in f]
        except FileNotFoundError:
            return []

    def remote(self, *args):
        return subprocess.run(["git", "--git-dir", self.fake.bare("bench-fw")] + list(args),
                              capture_output=True).stdout.decode().strip()

    def close(self):
        for c in (getattr(self, "db", None), getattr(self, "state", None)):
            if c is not None:
                c.close()
        self.fake.stop()
        self.endpoint.stop()


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.w = World(self._td.name)

    def tearDown(self):
        self.w.close()
        self._td.cleanup()

    def run_fix(self, iid=IID, **kw):
        out = io.StringIO()
        f = self.w.fixer(**kw)
        with mock.patch("sys.stdout", out):
            rc = f.run(iid)
        self.out = out.getvalue()
        return rc

    def alerts(self):
        conn = sqlite3.connect(self.w.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return {r["transition"]: dict(r) for r in
                    conn.execute("SELECT * FROM alerts WHERE incident_id=?", (IID,))}
        finally:
            conn.close()

    def fixes(self):
        return [dict(r) for r in self.w.state.execute("SELECT * FROM fixes ORDER BY id")]


class GoodFixTest(Base):
    def test_verified_fix_opens_one_pr(self):
        self.w.set_reply(reply(GOOD_DIFF))
        self.assertEqual(self.run_fix(), fix.EXIT_OK)
        prs = self.w.fake.pulls()
        self.assertEqual(len(prs), 1)
        pr = prs[0]
        self.assertEqual(pr["title"], "[lw-3] fix: keep demo_fill inside slots[]")
        self.assertEqual(pr["base"]["ref"], "main")
        branch = pr["head"]["ref"]
        self.assertRegex(branch, r"^lw-fix-3-[0-9a-f]{7}$")
        for want in ("src/demo.c:%d" % BAD_LINE, "-    for (int i = 0; i <= SLOTS; i++) {",
                     "LW BOOT fw=", "LW TEST demo_fill PASS", "LW RESULT PASS",
                     "LW HEALTH 1 loopback=OK", "LW HEALTH 3 loopback=OK", self.w.bad,
                     "never merges"):
            self.assertIn(want, pr["body"])
        # The pushed branch: one commit on the bad sha, by the bench identity, src/ only.
        fix_sha = self.w.remote("rev-parse", "refs/heads/" + branch)
        self.assertEqual(self.w.remote("rev-parse", fix_sha + "^"), self.w.bad)
        self.assertEqual(self.w.remote("log", "-1", "--format=%an <%ae>", fix_sha),
                         "Lab Watch bench <lw@example.invalid>")
        self.assertEqual(self.w.remote("diff", "--name-only", self.w.bad, fix_sha), "src/demo.c")
        self.assertEqual(self.w.remote("rev-parse", "refs/heads/main"), self.w.bad)
        # Flashed the fix, then main again; the hold is back to free.
        self.assertEqual(self.w.flashed, [(fix_sha, True), (self.w.bad, False)])
        self.assertFalse(os.path.exists(self.w.hold))
        # The PR table and the outbox row.
        row = self.fixes()[-1]
        self.assertEqual((row["status"], row["pr_number"], row["fix_sha"], row["branch"],
                          row["board_main"]), ("pr_open", pr["number"], fix_sha, branch,
                                               self.w.bad))
        a = self.alerts()
        self.assertEqual(a["fix"]["reply_to_alert_id"], a["open"]["id"])
        self.assertIsNone(a["fix"]["sent"])
        self.assertIn(pr["html_url"], a["fix"]["text"])
        self.assertTrue(a["fix"]["text"].startswith("LAB WATCH incident 3 fix proposed"))
        self.assertIn("PR #%d" % pr["number"], self.out)

    def test_rerun_with_pr_open_does_nothing(self):
        self.w.set_reply(reply(GOOD_DIFF))
        self.assertEqual(self.run_fix(), fix.EXIT_OK)
        calls = len(self.w.agent_calls())
        self.assertEqual(self.run_fix(), fix.EXIT_OK)
        self.assertEqual(len(self.w.agent_calls()), calls)
        self.assertEqual(len(self.w.fake.pulls()), 1)

    def test_agent_gets_evidence_fresh_session_and_no_token(self):
        self.w.set_reply("garbage")
        self.run_fix()
        self.w.set_reply(reply(GOOD_DIFF))
        self.run_fix()
        calls = self.w.agent_calls()
        self.assertEqual(len(calls), 2)
        sessions = [c["argv"][c["argv"].index("--session") + 1] for c in calls]
        self.assertNotEqual(sessions[0], sessions[1])
        msg = calls[1]["argv"][calls[1]["argv"].index("--message") + 1]
        for want in ("src/demo.c:%d" % BAD_LINE, "for (int i = 0; i <= SLOTS; i++) {",
                     "demo: fill one more", "LW-FIX-BEGIN", "EVIDENCE-BEGIN", self.w.bad[:7]):
            self.assertIn(want, msg)
        blob = json.dumps(calls)
        for tok in (SETUP_TOKEN, BENCH_TOKEN, ENDPOINT_TOKEN):
            self.assertNotIn(tok, blob)

    def test_fenced_diff_is_accepted(self):
        self.w.set_reply(reply("```diff\n" + GOOD_DIFF + "```\n"))
        self.assertEqual(self.run_fix(), fix.EXIT_OK)

    def test_issue_gets_a_queued_comment_when_mirrored(self):
        self.w.fixer()  # creates the state schema
        self.w.state.execute("INSERT INTO issues (key, incident_id, number) VALUES ('lw-3', 3, 1)")
        self.w.state.commit()
        self.w.set_reply(reply(GOOD_DIFF))
        self.assertEqual(self.run_fix(), fix.EXIT_OK)
        ops = self.w.state.execute("SELECT op, body FROM outbox WHERE key='lw-3'").fetchall()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["op"], "comment")
        self.assertIn("PR #", ops[0]["body"])


class FailedFixTest(Base):
    def test_failed_verification_no_pr_board_back_on_main(self):
        self.w.set_reply(reply(GOOD_DIFF))
        self.w.flash_mode = "fail"
        self.assertEqual(self.run_fix(), fix.EXIT_FAILED)
        self.assertEqual(self.w.fake.pulls(), [])
        self.assertEqual(self.w.remote("branch", "--list"), "")
        self.assertEqual([s for s, _ in self.w.flashed][1], self.w.bad)
        self.assertEqual(self.fixes()[-1]["status"], "failed")
        a = self.alerts()
        self.assertNotIn("fix", a)
        self.assertIn("fix failed verification", a["fix-failed"]["text"])
        self.assertIn("LW RESULT FAIL", a["fix-failed"]["text"])
        self.assertIn("No pull request", a["fix-failed"]["text"])
        self.assertEqual(a["fix-failed"]["reply_to_alert_id"], a["open"]["id"])
        self.assertIn("fix failed verification", self.out)
        self.assertFalse(os.path.exists(self.w.hold))

    def test_flash_error_is_failed_verification_and_says_board_not_back(self):
        self.w.set_reply(reply(GOOD_DIFF))
        self.w.flash_mode = "error"
        self.assertEqual(self.run_fix(), fix.EXIT_FAILED)
        self.assertEqual(self.w.fake.pulls(), [])
        self.assertIn("NOT back on main", self.alerts()["fix-failed"]["text"])

    def test_lane_a_flash_missing_is_failed_verification(self):
        self.w.set_reply(reply(GOOD_DIFF))
        with mock.patch.dict(sys.modules, {"bench.flash": None}):
            rc = self.run_fix(verifier=fix.lane_a_verifier(5.0))
        self.assertEqual(rc, fix.EXIT_FAILED)
        self.assertIn("no flash step", self.alerts()["fix-failed"]["text"])

    def test_bench_held_flashes_nothing(self):
        with open(self.w.hold, "w") as f:
            f.write("lane D accuracy loop\n")
        self.w.set_reply(reply(GOOD_DIFF))
        with self.assertRaises(fix.FixError) as cm:
            self.run_fix()
        self.assertEqual(cm.exception.code, fix.EXIT_HELD)
        self.assertEqual(self.w.flashed, [])
        with open(self.w.hold) as f:
            self.assertEqual(f.read(), "lane D accuracy loop\n")

    def test_push_refused_is_exit_5_and_kept_for_retry(self):
        self.w.set_reply(reply(GOOD_DIFF))
        f = self.w.fixer()
        f.fj = Forgejo(os.path.join(self.w.secrets, "tracker_token"), base=self.w.fake.base,
                       repo="labwatch/bench-fw")  # the bench token: no repository scope
        self.w.fake._new_repo("bench-fw")
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(f.run(IID), fix.EXIT_PUSH)
        self.assertEqual(self.fixes()[-1]["status"], "push_failed")
        self.assertNotIn("fix", self.alerts())


class RejectTest(Base):
    def assertRejected(self, text, why):
        self.w.set_reply(text)
        self.assertEqual(self.run_fix(), fix.EXIT_REJECTED)
        self.assertEqual(self.w.flashed, [])
        self.assertEqual(self.w.fake.pulls(), [])
        row = self.fixes()[-1]
        self.assertEqual(row["status"], "rejected")
        self.assertIn(why, row["reason"])
        self.assertIn("agent diff rejected", self.alerts()["fix-failed"]["text"])

    def test_path_outside_src(self):
        d = GOOD_DIFF + "--- a/README\n+++ b/README\n@@ -1 +1 @@\n-invented bench repo\n+x\n"
        self.assertRejected(reply(d), "outside src/")

    def test_path_traversal(self):
        self.assertRejected(reply(GOOD_DIFF.replace("src/demo.c", "src/../README")),
                            "outside src/")

    def test_does_not_apply(self):
        self.assertRejected(reply(GOOD_DIFF.replace("i <= SLOTS", "j <= SLOTS")),
                            "does not apply")

    def test_too_big(self):
        big = "--- a/src/demo.c\n+++ b/src/demo.c\n@@ -12,2 +12,43 @@\n     }\n }\n" + \
              "".join("+/* line %d */\n" % n for n in range(41))
        self.assertRejected(reply(big), "more than 40")

    def test_garbage(self):
        self.assertRejected("I think the code is fine, no change needed.\n", "no single")

    def test_no_summary(self):
        self.assertRejected("%s\n%s%s\n" % (fix.BEGIN, GOOD_DIFF, fix.END), "SUMMARY")

    def test_new_file(self):
        d = "--- /dev/null\n+++ b/src/new.c\n@@ -0,0 +1 @@\n+int x;\n"
        self.assertRejected(reply(d), "creates or deletes")

    def test_must_touch_cited_file(self):
        self.w.set_reply("")  # unused
        with self.assertRaises(fix.Rejected):
            fix.validate_diff(GOOD_DIFF.replace("demo.c", "other.c"), 40, "src/demo.c")


class EligibleTest(Base):
    def test_hardware_incident_is_not_touched(self):
        self.w.set_reply(reply(GOOD_DIFF))
        with self.assertRaises(fix.FixError) as cm:
            self.run_fix(iid=4)
        self.assertEqual(cm.exception.code, fix.EXIT_NOT_ELIGIBLE)
        self.assertEqual(self.w.agent_calls(), [])

    def test_file_line_outside_src(self):
        with self.assertRaises(fix.FixError):
            self.run_fix(iid=5)
        self.assertEqual(self.w.agent_calls(), [])

    def test_never_the_full_clone(self):
        home = os.path.join(self.w.tmp, "home")
        os.makedirs(os.path.join(home, "fw"))
        subprocess.run(["git", "init", "--quiet", os.path.join(home, "fw")], check=True)
        with mock.patch.dict(os.environ, {"HOME": home}):
            for p in ("~/fw", os.path.join(home, "fw"), os.path.join(home, "fw", "src")):
                with self.assertRaises(fix.FixError):
                    fix.check_bench_repo(p)
            rc = fix.main(["--incident", "3", "--fw-repo", "~/fw", "--config", self.w.config,
                           "--db", self.w.db_path])
        self.assertEqual(rc, fix.EXIT_NOT_ELIGIBLE)


class DiffAndSerialUnitTest(unittest.TestCase):
    def test_validate_good(self):
        d, paths, n = fix.validate_diff(GOOD_DIFF, 40, "src/demo.c")
        self.assertEqual((paths, n), (["src/demo.c"], 2))

    def test_validate_refusals(self):
        bad = {
            "rename": "diff --git a/src/a.c b/src/b.c\nrename from src/a.c\nrename to src/b.c\n",
            "binary": "diff --git a/src/a.c b/src/a.c\nGIT binary patch\nliteral 0\n",
            "mode": "diff --git a/src/demo.c b/src/demo.c\nold mode 100644\nnew mode 100755\n",
            "absolute": GOOD_DIFF.replace("a/src/demo.c", "/etc/passwd"),
            "noprefix": GOOD_DIFF.replace("a/src/demo.c", "src/demo.c"),
            "dotdir": GOOD_DIFF.replace("src/demo.c", "src/./demo.c"),
            "empty": "",
            "nochange": "--- a/src/demo.c\n+++ b/src/demo.c\n@@ -1 +1 @@\n /* x */\n",
        }
        for name, d in bad.items():
            with self.subTest(name):
                with self.assertRaises(fix.Rejected):
                    fix.validate_diff(d, 40, "src/demo.c")

    def test_parse_reply_needs_one_block(self):
        two = reply(GOOD_DIFF) + reply(GOOD_DIFF)
        with self.assertRaises(fix.Rejected):
            fix.parse_reply(two)
        with self.assertRaises(fix.Rejected):
            fix.parse_reply(reply(GOOD_DIFF, summary="x" * 61))

    def test_check_serial(self):
        sha = "abcdef0123"
        ok = ["LW BOOT fw=abcdef0 reset=PIN csr=0x0", "LW TEST t PASS", "LW RESULT PASS"] + \
             ["LW HEALTH %d loopback=OK" % n for n in (1, 2, 3)]
        self.assertTrue(fix.check_serial(ok, sha)[0])
        self.assertTrue(fix.check_serial(["LW BOOT fw=1111111 reset=PIN csr=0x0"] + ok, sha)[0])
        cases = {
            "no LW BOOT": ok[1:],
            "no LW BOOT fw=": ["LW BOOT fw=1111111 reset=PIN csr=0x0"] + ok[1:],
            "HardFault": ok[:2] + ["LW HARDFAULT pc=0x1"],
            "RESULT FAIL": ok[:2] + ["LW RESULT FAIL"],
            "only 2 of 3": ok[:5],
            "loopback=OPEN": ok[:4] + ["LW HEALTH 2 loopback=OPEN"],
            "booted again": ok[:4] + [ok[0]] + ok[1:],
            "no LW RESULT": ok[:2],
        }
        for why, lines in cases.items():
            with self.subTest(why):
                good, reason, _ = fix.check_serial(lines, sha)
                self.assertFalse(good)
                self.assertIn(why, reason)


class ScopeTest(Base):
    """The bench token (write:issue) cannot push or open PRs; the setup token can."""

    def setUp(self):
        super().setUp()
        self.bench = Forgejo(os.path.join(self.w.secrets, "tracker_token"),
                             base=self.w.fake.base, repo="labwatch/bench-fw")
        self.setup = Forgejo(os.path.join(self.w.secrets, "tracker_setup_token"),
                             base=self.w.fake.base, repo="labwatch/bench-fw")

    def test_bench_token_refused_setup_token_allowed(self):
        with self.assertRaises(TrackerError) as cm:
            self.bench.ensure_repo()
        self.assertEqual(cm.exception.status, 403)
        self.assertTrue(self.setup.ensure_repo())
        self.assertFalse(self.setup.ensure_repo())
        with self.assertRaises(TrackerError) as cm:
            self.bench.push(self.w.bare, ["refs/heads/main:refs/heads/main"])
        self.assertEqual(cm.exception.status, 403)
        self.setup.push(self.w.bare, ["refs/heads/main:refs/heads/main"])
        self.assertEqual(self.w.remote("rev-parse", "refs/heads/main"), self.w.bad)
        with self.assertRaises(TrackerError) as cm:
            self.bench.create_pull("t", "b", "main", "main")
        self.assertEqual(cm.exception.status, 403)
        out = []
        fix.probe_scope(self.w.fake.base, self.w.secrets, "labwatch/bench-fw", self.w.bare,
                        out=out.append)
        self.assertIn("push refused (HTTP 403); PR create refused (HTTP 403)", out[0])
        self.assertIn("tracker_setup_token", out[0])

    def test_probe_writes_nothing_and_sees_a_scoped_token(self):
        self.setup.ensure_repo()
        self.setup.push(self.w.bare, ["refs/heads/main:refs/heads/main"])
        sdir = os.path.join(self.w.tmp, "scoped")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "tracker_token"), "w") as f:
            f.write(SETUP_TOKEN + "\n")  # a token that has repository scope
        out = []
        self.assertEqual(fix.probe_scope(self.w.fake.base, sdir, "labwatch/bench-fw",
                                         self.w.bare, out=out.append), 0)
        self.assertIn("push allowed; PR create allowed (HTTP 404", out[0])
        self.assertEqual(self.w.remote("for-each-ref", "--format=%(refname)"), "refs/heads/main")
        self.assertEqual(self.w.fake.pulls(), [])

    def test_push_token_never_in_argv_only_in_git_env(self):
        self.setup.ensure_repo()
        seen = []
        real = subprocess.run

        def spy(argv, *a, **kw):
            seen.append((list(argv), dict(kw.get("env") or {})))
            return real(argv, *a, **kw)

        with mock.patch("tracker.forgejo.subprocess.run", side_effect=spy):
            self.setup.push(self.w.bare, ["refs/heads/main:refs/heads/main"])
        argv, env = seen[0]
        self.assertNotIn(SETUP_TOKEN, " ".join(argv))
        self.assertNotIn("@", self.setup.git_url())
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
        self.assertIn(self.w.fake.auth_seen[-1], env["GIT_CONFIG_VALUE_0"])
        self.assertNotIn(SETUP_TOKEN, json.dumps(self.w.fake.calls))

    def test_no_force_and_no_rewrite_of_remote_main(self):
        self.setup.ensure_repo()
        with self.assertRaises(TrackerError):
            self.setup.push(self.w.bare, ["+refs/heads/main:refs/heads/main"])
        self.setup.push(self.w.bare, ["refs/heads/main:refs/heads/main"])
        with self.assertRaises(TrackerError) as cm:  # not a fast-forward
            self.setup.push(self.w.bare, ["%s:refs/heads/main" % self.w.good])
        self.assertIn("not a fast-forward", str(cm.exception))
        self.assertEqual(self.w.remote("rev-parse", "refs/heads/main"), self.w.bad)


def advance_remote_main(w):
    """A human merged a fix: the code repo's main gets a commit bench main does not have."""
    work = os.path.join(w.tmp, "merge-work")
    subprocess.run(["git", "clone", "--quiet", w.fake.bare("bench-fw"), work], check=True)
    with open(os.path.join(work, "README"), "a") as f:
        f.write("merged elsewhere\n")
    run(work, "-c", "user.name=Human", "-c", "user.email=h@example.invalid", "commit",
        "--quiet", "-am", "merged change")
    run(work, "push", "--quiet", "origin", "HEAD:refs/heads/main")
    return run(work, "rev-parse", "HEAD")


class RemoteMainAheadTest(Base):
    def test_setup_and_fix_still_work_after_a_merge(self):
        from tracker import setup_api
        env = {"LW_TRACKER": self.w.fake.base}
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(setup_api.main(["--secrets-dir", self.w.secrets,
                                             "--fw-repo", self.w.bare]), 0)
        ahead = advance_remote_main(self.w)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdout", out):
            self.assertEqual(setup_api.main(["--secrets-dir", self.w.secrets,
                                             "--fw-repo", self.w.bare]), 0)
        self.assertIn("ahead", out.getvalue())
        self.w.set_reply(reply(GOOD_DIFF))
        self.assertEqual(self.run_fix(), fix.EXIT_OK)
        self.assertEqual(len(self.w.fake.pulls()), 1)
        self.assertEqual(self.w.remote("rev-parse", "refs/heads/main"), ahead)


class SetupCodeRepoTest(Base):
    def test_setup_creates_code_repo_and_pushes_main_idempotently(self):
        from tracker import setup_api
        outs = []
        for _ in range(2):
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"LW_TRACKER": self.w.fake.base}), \
                    mock.patch("sys.stdout", out), mock.patch("sys.stderr", out):
                rc = setup_api.main(["--secrets-dir", self.w.secrets, "--fw-repo", self.w.bare])
            self.assertEqual(rc, 0, out.getvalue())
            outs.append(out.getvalue())
        self.assertIn("repo labwatch/bench-fw: created", outs[0])
        self.assertIn("repo labwatch/bench-fw: exists", outs[1])
        self.assertEqual(self.w.remote("rev-parse", "refs/heads/main"), self.w.bad)
        for tok in (SETUP_TOKEN, BENCH_TOKEN):
            self.assertNotIn(tok, "".join(outs))


if __name__ == "__main__":
    unittest.main()
