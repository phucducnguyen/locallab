"""Lane C: setup_api, the Forgejo client and the mirror, against the fake Forgejo."""

import contextlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from stub import make_db
from stub.db import Writer, connect as stub_connect
from tests.test_c_fake_forgejo import BENCH_TOKEN, SETUP_TOKEN, FakeForgejo
from tracker import mirror, setup_api
from tracker.forgejo import Forgejo, TrackerError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write(path, text):
    with open(path, "w", encoding="ascii") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Base(unittest.TestCase):
    lag = 0.5

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.secrets = os.path.join(d, "secrets")
        os.mkdir(self.secrets, 0o700)
        _write(os.path.join(self.secrets, "tracker_setup_token"), SETUP_TOKEN + "\n")
        _write(os.path.join(self.secrets, "tracker_token"), BENCH_TOKEN + "\n")
        self.labels_path = os.path.join(self.secrets, "tracker_labels.json")
        self.fake = FakeForgejo(search_lag=self.lag).start()
        self.setup_fj = Forgejo(os.path.join(self.secrets, "tracker_setup_token"),
                                base=self.fake.base)
        self.bench_fj = Forgejo(os.path.join(self.secrets, "tracker_token"), base=self.fake.base)
        self.db = os.path.join(d, "bench.db")
        self.state_path = os.path.join(d, "tracker_state.db")
        self.logs = []

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def do_setup(self):
        return setup_api.setup(self.setup_fj, self.labels_path, log=lambda s: None)

    def mirror(self, fj=None, wait=3.0, step=0.1, wall=time.time):
        conn = mirror.connect(self.db)
        self.addCleanup(conn.close)
        state = mirror.connect_state(self.state_path)
        self.addCleanup(state.close)
        return mirror.Mirror(conn, fj or self.bench_fj, mirror.load_labels(self.labels_path),
                             state, search_wait=wait, search_step=step, wall=wall,
                             log=self.logs.append)

    def outbox(self, pending_only=False):
        conn = mirror.connect_state(self.state_path)
        try:
            sql = "SELECT * FROM outbox" + (" WHERE done IS NULL" if pending_only else "")
            return [dict(r) for r in conn.execute(sql + " ORDER BY id").fetchall()]
        finally:
            conn.close()

    @staticmethod
    def creates(done):
        return [d for d in done if d[1] == "create"]

    def rows(self, sql, *args):
        conn = mirror.connect(self.db)
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()


class SetupApiTest(Base):
    def test_creates_repo_labels_and_ids_file(self):
        ids = self.do_setup()
        self.assertEqual(ids, {"firmware": 2, "hardware": 1, "needs-triage": 3})
        with open(self.labels_path) as f:
            self.assertEqual(json.load(f), ids)
        self.assertEqual(stat.S_IMODE(os.stat(self.labels_path).st_mode), 0o600)

    def test_second_run_changes_nothing(self):
        self.do_setup()
        before = os.stat(self.labels_path).st_mtime_ns
        n_calls = len(self.fake.calls)
        log = []
        setup_api.setup(self.setup_fj, self.labels_path, log=log.append)
        writes = [c for c in self.fake.calls[n_calls:] if c[0] != "GET"]
        self.assertEqual(writes, [])
        self.assertEqual(os.stat(self.labels_path).st_mtime_ns, before)
        self.assertTrue(all("created" not in s for s in log), log)

    def test_reads_ids_back_rather_than_assuming(self):
        self.fake.repos["bench-1"] = {"labels": [{"id": 7, "name": "other", "color": ""}],
                                      "issues": []}
        self.assertEqual(self.do_setup(), {"hardware": 8, "firmware": 9, "needs-triage": 10})

    def test_bench_token_refused_repo_creation(self):
        with self.assertRaises(TrackerError) as cm:
            self.bench_fj.create_repo("bench-2")
        self.assertEqual(cm.exception.status, 403)

    def test_cli_prints_no_token(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"LW_TRACKER": self.fake.base}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = setup_api.main(["--secrets-dir", self.secrets])
        self.assertEqual(rc, 0)
        self.assertNotIn(SETUP_TOKEN, out.getvalue() + err.getvalue())
        self.assertNotIn(BENCH_TOKEN, out.getvalue() + err.getvalue())


class ClientTest(Base):
    def test_four_calls(self):
        self.do_setup()
        n = self.bench_fj.create_issue("[lw-9] t", "Routed to: Triage", [3])
        self.bench_fj.comment(n, "flaps: 2")
        self.bench_fj.close(n)
        issue = self.fake.issues()[0]
        self.assertEqual((issue["state"], issue["comments"]), ("closed", ["flaps: 2"]))
        self.assertEqual([lb["name"] for lb in issue["labels"]], ["needs-triage"])
        time.sleep(self.lag + 0.1)
        self.assertEqual([i["number"] for i in self.bench_fj.search(q="lw-9")], [n])
        self.assertEqual([i["number"] for i in self.bench_fj.search(labels="needs-triage")], [n])

    def test_bad_token_error_hides_token(self):
        self.do_setup()
        bad = os.path.join(self.secrets, "bad")
        _write(bad, "fake-wrong-token-xyz")
        with self.assertRaises(TrackerError) as cm:
            Forgejo(bad, base=self.fake.base).search(q="x")
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn("fake-wrong-token-xyz", str(cm.exception))

    def test_unreachable_is_refused(self):
        fj = Forgejo(os.path.join(self.secrets, "tracker_token"),
                     base="http://127.0.0.1:%d" % _free_port(), timeout=1)
        with self.assertRaises(TrackerError) as cm:
            fj.search(q="x")
        self.assertIsNone(cm.exception.status)
        self.assertTrue(cm.exception.refused)

    def test_base_url_from_env(self):
        with mock.patch.dict(os.environ, {"LW_TRACKER": self.fake.base + "/"}):
            self.assertEqual(Forgejo("x").base, self.fake.base)
        with mock.patch.dict(os.environ):
            os.environ.pop("LW_TRACKER", None)
            self.assertEqual(Forgejo("x").base, "http://127.0.0.1:3000")


class MirrorStubDbTest(Base):
    def setUp(self):
        super().setUp()
        make_db.build(self.db)
        self.do_setup()

    def test_one_issue_per_routing_row_with_number_stored(self):
        done = self.creates(self.mirror().once())
        self.assertEqual([d[3] for d in done], ["created"] * 3)
        issues = {i["number"]: i for i in self.fake.issues()}
        self.assertEqual(len(issues), 3)
        for r in self.rows("SELECT r.id, r.ticket, r.incident_id, i.ticket AS iticket, i.board, "
                           "i.kind, i.triage, r.route FROM routing r JOIN incidents i "
                           "ON i.id = r.incident_id"):
            self.assertIsNotNone(r["ticket"])
            self.assertEqual(r["ticket"], r["iticket"])
            issue = issues[int(r["ticket"])]
            self.assertEqual(issue["title"], "[lw-%d] %s %s: %s" % (
                r["incident_id"], r["board"], r["kind"], r["triage"]))
            self.assertIn("Routed to: %s" % r["route"], issue["body"].splitlines())
            want = {"Hardware team": "hardware", "Firmware team": "firmware",
                    "Triage": "needs-triage"}[r["route"]]
            self.assertEqual([lb["name"] for lb in issue["labels"]], [want])

    def test_hardfault_issue_has_sha_and_file_line_and_no_source(self):
        self.mirror().once()
        hf = [i for i in self.fake.issues() if "HARDFAULT" in i["title"]]
        self.assertEqual(len(hf), 1)
        body = hf[0]["body"]
        self.assertEqual([lb["name"] for lb in hf[0]["labels"]], ["firmware"])
        self.assertIn("Routed to: Firmware team", body)
        self.assertIn("Verified sha: 2222222", body)
        self.assertIn("File: src/demo.c:42", body)
        for bad in ("LW HARDFAULT", "diff --git", "@@", "+++", "subject", "demo: add"):
            self.assertNotIn(bad, body)
        jumper = [i for i in self.fake.issues() if i["title"].startswith("[lw-1]")][0]
        self.assertIn("Routed to: Hardware team", jumper["body"])
        self.assertIn("Verified sha: 1111111", jumper["body"])

    def test_second_pass_and_restart_create_nothing(self):
        self.mirror().once()
        n_calls = len(self.fake.calls)
        self.assertEqual(self.mirror().once(), [])
        self.assertEqual(self.fake.calls[n_calls:], [])
        self.assertEqual(len(self.fake.issues()), 3)

    def test_live_jumper_from_emit(self):
        self.mirror().once()
        out = subprocess.run([sys.executable, "-m", "stub.emit", "--db", self.db, "--scenario",
                              "jumper", "--speed", "0"], cwd=ROOT, capture_output=True,
                             text=True, check=True)
        iid = int(out.stdout.strip())
        done = self.creates(self.mirror().once())
        self.assertEqual(len(done), 1)
        issue = self.fake.issues()[-1]
        self.assertTrue(issue["title"].startswith("[lw-%d] nucleo-1 HEALTH_FAIL: HARDWARE"
                                                  % iid))
        self.assertEqual([lb["name"] for lb in issue["labels"]], ["hardware"])
        self.assertEqual(self.rows("SELECT ticket FROM incidents WHERE id=?", iid)[0]["ticket"],
                         str(issue["number"]))

    def test_second_routing_row_of_same_incident_links(self):
        self.mirror().once()
        conn = stub_connect(self.db)
        Writer(conn).route(time.time(), "CODE", "again", incident_id=3, run_id=2)
        conn.close()
        done = self.mirror().once()
        self.assertEqual([d[1:] for d in done], [("link", 3, "linked")])
        self.assertEqual(len(self.fake.issues()), 3)

    def test_run_only_routing_row(self):
        conn = stub_connect(self.db)
        Writer(conn).route(time.time(), "UNKNOWN", "no output", run_id=2)
        conn.close()
        self.mirror().once()
        titles = [i["title"] for i in self.fake.issues()]
        self.assertIn("[lw-run-2] run HARDFAULT: UNKNOWN", titles)

    def test_crash_after_create_no_second_issue(self):
        self.fake.drop_reply_once = True  # issue created, reply lost (bench killed / timeout)
        done = self.mirror().once()
        self.assertEqual(done, [])
        self.assertEqual(len(self.fake.issues()), 1)
        self.assertEqual(self.rows("SELECT ticket FROM routing WHERE id=1")[0]["ticket"], None)
        head = self.outbox(pending_only=True)[0]
        self.assertEqual((head["key"], head["op"]), ("lw-1", "create"))
        self.assertIsNotNone(head["sent"])
        # Restart at once: search lags, the mirror waits and finds the exact title.
        done = self.creates(self.mirror().once())
        self.assertEqual(done[0], ("lw-1", "create", 1, "found"))
        self.assertEqual(len(self.fake.issues()), 3)
        self.assertEqual(self.outbox(pending_only=True), [])

    def test_pending_without_issue_ignores_fuzzy_hits_then_creates(self):
        # A row whose title only fuzzily matches must not be taken for ours.
        self.bench_fj.create_issue("[lw-12] nucleo-1 HEALTH_FAIL: HARDWARE", "lw-1", [1])
        time.sleep(self.lag + 0.1)
        m = self.mirror(wait=0.4)
        m.plan()
        m.state.execute("UPDATE outbox SET sent=1 WHERE key='lw-1' AND op='create'")
        m.state.commit()
        done = self.creates(m.replay())
        self.assertEqual(done[0][1:], ("create", 2, "created"))
        self.assertEqual(len(self.fake.issues()), 4)

    def test_tracker_down_queues_then_creates(self):
        down = Forgejo(os.path.join(self.secrets, "tracker_token"),
                       base="http://127.0.0.1:%d" % _free_port(), timeout=1)
        self.assertEqual(self.mirror(fj=down).once(), [])
        self.assertTrue(all(o["sent"] is None for o in self.outbox()))
        self.assertEqual(len(self.creates(self.mirror().once())), 3)

    def test_server_error_keeps_row_pending(self):
        self.fake.fail_next = [500]
        self.assertEqual(self.mirror(wait=0.3).once(), [])
        self.assertIsNotNone(self.outbox(pending_only=True)[0]["sent"])  # outcome unknown
        self.assertEqual(len(self.creates(self.mirror(wait=0.3).once())), 3)
        self.assertEqual(len(self.fake.issues()), 3)

    def test_cli_once(self):
        env = dict(os.environ, LW_TRACKER=self.fake.base)
        out = subprocess.run([sys.executable, "-m", "tracker.mirror", "--db", self.db,
                              "--secrets-dir", self.secrets, "--once"], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.count("(created)"), 3)
        self.assertNotIn(BENCH_TOKEN, out.stdout + out.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "tracker_state.db")))


class IssueTextTest(unittest.TestCase):
    def test_reason_flattened_and_bad_fields_dropped(self):
        row = {"id": 5, "incident_id": 4, "run_id": None, "triage": "CODE",
               "route": "Firmware team", "reason": "line one\nline two\x07",
               "board": "nucleo-1", "kind": "HARDFAULT", "firmware": "not a sha",
               "i_evidence": json.dumps({"file_line": "int x = 1; // src.c:3"}),
               "run_commit": None, "verdict": None, "r_evidence": None}
        key, title, body = mirror.issue_text(row)
        self.assertEqual(key, "lw-4")
        self.assertIn("Reason: line one line two", body)
        self.assertIn("Verified sha: none", body)
        self.assertNotIn("File:", body)
        self.assertTrue(all(" " <= c <= "~" for c in body.replace("\n", "")))


class ScriptTest(unittest.TestCase):
    def test_setup_script_syntax(self):
        subprocess.run(["bash", "-n", os.path.join(ROOT, "tracker", "setup_forgejo.sh")],
                       check=True)

    def test_setup_script_never_pulls_and_binds_localhost(self):
        with open(os.path.join(ROOT, "tracker", "setup_forgejo.sh")) as f:
            text = f.read()
        self.assertNotIn("docker pull", text)
        self.assertIn("--pull never", text)
        self.assertIn("-p 127.0.0.1:3000:3000", text)
        self.assertIn("16.0.6-rootless", text)

    def test_setup_script_keeps_password_out_of_host_argv(self):
        with open(os.path.join(ROOT, "tracker", "setup_forgejo.sh")) as f:
            text = f.read()
        self.assertIn("--random-password", text)
        self.assertNotIn('--password "$(', text)
        self.assertIn('docker exec -i "$NAME" sh -c', text)  # password read inside, from stdin
        self.assertIn(".lw-tracker-token", text)  # the offline installer's setup is detected


if __name__ == "__main__":
    unittest.main()
