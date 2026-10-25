"""Lane C stage 2: never duplicate, flaps, close on resolve, the queue (fake Forgejo)."""

import os
import signal
import subprocess
import sys
import unittest

from stub import make_db
from stub.db import Writer, connect as stub_connect, init as stub_init
from tests.test_c_mirror import BENCH_TOKEN, ROOT, Base, _free_port
from tracker.forgejo import Forgejo

T0 = 1_800_000_000.0
MARK = "(lab watch update "


class Clocked(Base):
    """An empty bench DB written with the stub's Writer, and a hand-driven wall clock."""

    def setUp(self):
        super().setUp()
        self.do_setup()
        self.bench = stub_connect(self.db)
        stub_init(self.bench)
        self.addCleanup(self.bench.close)
        self.w = Writer(self.bench)
        self.now = T0
        self.fake.calls.clear()

    def m(self, fj=None):
        return self.mirror(fj=fj, wall=lambda: self.now)

    def down(self):
        return Forgejo(os.path.join(self.secrets, "tracker_token"),
                       base="http://127.0.0.1:%d" % _free_port(), timeout=1)

    def pull(self):
        # One board per pull: these are per-incident tests; the same problem on one
        # board shares an issue (C4, tests/test_c_reuse.py).
        self.w.board = "nucleo-%d" % (self.bench.execute("SELECT COUNT(*) FROM incidents")
                                      .fetchone()[0] + 1)
        return self.w.open_incident(self.now, "HEALTH_FAIL", "HARDWARE",
                                    "loopback=OPEN on 2 lines in a row.", {"lines": []})

    def flap(self, iid, k):
        self.w.update_incident(iid, state="FLAPPING", flaps=k, closed=None)

    def resolve(self, iid):
        self.w.resolve(self.now, iid)

    def comments(self, n=1):
        return self.fake.issues()[n - 1]["comments"]

    def writes(self):
        return [(c[0], c[1].rsplit("/bench-1", 1)[-1]) for c in self.fake.calls if c[0] != "GET"]


class CrashTest(Base):
    def setUp(self):
        super().setUp()
        make_db.build(self.db)
        conn = stub_connect(self.db)  # the flap on a second board: three problems (C4)
        conn.execute("UPDATE incidents SET board='nucleo-2' WHERE id=2")
        conn.commit()
        conn.close()
        self.do_setup()

    def test_killed_right_after_create_then_restart_one_issue(self):
        env = dict(os.environ, LW_TRACKER=self.fake.base)
        proc = subprocess.Popen([sys.executable, "-m", "tracker.mirror", "--db", self.db,
                                 "--secrets-dir", self.secrets, "--state", self.state_path],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.fake.drop_reply_once = True
        self.fake.on_create = lambda issue: os.kill(proc.pid, signal.SIGKILL)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertNotIn(BENCH_TOKEN, out + err)
        self.fake.on_create = None
        self.assertEqual(len(self.fake.issues()), 1)
        self.assertIsNone(self.rows("SELECT ticket FROM incidents WHERE id=1")[0]["ticket"])
        # Restart at once, inside the indexer lag: the mirror waits for the exact title.
        done = self.creates(self.mirror().once())
        self.assertEqual(done[0], ("lw-1", "create", 1, "found"))
        titles = [i["title"] for i in self.fake.issues()]
        self.assertEqual(len(titles), 3)
        self.assertEqual(len(set(titles)), 3)
        self.assertEqual(self.rows("SELECT ticket FROM routing WHERE incident_id=1")[0]["ticket"],
                         "1")
        self.assertEqual(self.mirror().once(), [])
        self.assertEqual(len(self.fake.issues()), 3)

    def test_number_stored_in_bench_but_not_in_state_is_reused(self):
        m = self.mirror()
        m.plan()
        self.bench_fj.create_issue("[lw-1] nucleo-1 HEALTH_FAIL: HARDWARE", "x", [1])
        conn = stub_connect(self.db)
        conn.execute("UPDATE incidents SET ticket='1' WHERE id=1")
        conn.commit()
        conn.close()
        m.state.execute("UPDATE outbox SET sent=1 WHERE key='lw-1'")
        m.state.commit()
        done = self.creates(m.replay())
        self.assertEqual(done[0], ("lw-1", "create", 1, "found"))
        self.assertEqual(len(self.fake.issues()), 3)


class FlapCloseTest(Clocked):
    def test_resolve_comments_duration_then_closes(self):
        iid = self.pull()
        self.m().once()
        self.now += 7.5
        self.resolve(iid)
        self.m().once()
        issue = self.fake.issues()[0]
        self.assertEqual(issue["state"], "closed")
        self.assertEqual(len(issue["comments"]), 1)
        self.assertTrue(issue["comments"][0].startswith("Resolved after 7.5 s."))
        self.assertEqual(self.writes()[-2:], [("POST", "/issues/1/comments"),
                                             ("PATCH", "/issues/1")])
        self.assertEqual(self.m().once(), [])

    def test_five_quick_flaps_one_comment_per_60_s(self):
        iid = self.pull()
        self.m().once()
        for k in range(1, 6):  # five quick pulls, 4 s apart, the mirror polling in between
            self.now += 2
            self.resolve(iid)
            self.m().once()
            self.now += 2
            self.flap(iid, k)
            self.m().once()
        flap_comments = [c for c in self.comments() if c.startswith("Flapping")]
        self.assertEqual(len(flap_comments), 1)
        self.assertTrue(flap_comments[0].startswith("Flapping: 1 flaps"))
        self.assertEqual(len(self.fake.issues()), 1)
        self.assertEqual(self.fake.issues()[0]["state"], "open")  # reopened after the 1st close
        # 60 s after the first flap comment, one more with the count so far.
        self.now += 44
        self.m().once()
        self.assertEqual(len(self.comments()), 2 + 1)  # resolve, flap 1, flap 5
        self.assertTrue(self.comments()[-1].startswith("Flapping: 5 flaps"))
        self.m().once()
        self.assertEqual(len(self.comments()), 3)
        # Resolved: closed only once the flap window has passed.
        self.resolve(iid)
        self.now += 30
        self.m().once()
        self.assertEqual(self.fake.issues()[0]["state"], "open")
        self.now += 31
        self.m().once()
        issue = self.fake.issues()[0]
        self.assertEqual(issue["state"], "closed")
        self.assertIn("Flaps: 5.", issue["comments"][-1])
        self.assertTrue(issue["comments"][-1].startswith("Resolved after "))
        self.assertTrue(all(MARK in c for c in issue["comments"]))

    def test_ambiguous_comment_not_doubled(self):
        iid = self.pull()
        self.m().once()
        self.now += 3
        self.resolve(iid)
        self.fake.drop_comment_reply_once = True
        self.m().once()
        self.assertEqual(len(self.comments()), 1)
        self.assertEqual(self.fake.issues()[0]["state"], "open")
        self.m().once()
        self.assertEqual(len(self.comments()), 1)
        self.assertEqual(self.fake.issues()[0]["state"], "closed")


class QueueTest(Clocked):
    def test_tracker_down_then_up_nothing_lost_nothing_doubled(self):
        a = self.pull()
        self.m(self.down()).once()
        self.now += 5
        self.resolve(a)
        self.m(self.down()).once()
        self.now += 1
        b = self.pull()
        self.m(self.down()).once()
        self.now += 2
        self.resolve(b)
        self.m(self.down()).once()
        self.now += 2
        self.flap(b, 1)
        self.m(self.down()).once()
        self.assertEqual(self.fake.calls, [])
        self.assertEqual([(o["key"], o["op"]) for o in self.outbox(pending_only=True)], [
            ("lw-%d" % a, "create"), ("lw-%d" % a, "comment"), ("lw-%d" % a, "close"),
            ("lw-%d" % b, "create"), ("lw-%d" % b, "comment"), ("lw-%d" % b, "close"),
            ("lw-%d" % b, "reopen"), ("lw-%d" % b, "comment")])
        self.assertTrue(all(o["sent"] is None for o in self.outbox()))
        self.m().once()  # up: replay in order
        self.assertEqual(self.writes(), [
            ("POST", "/issues"), ("POST", "/issues/1/comments"), ("PATCH", "/issues/1"),
            ("POST", "/issues"), ("POST", "/issues/2/comments"), ("PATCH", "/issues/2"),
            ("PATCH", "/issues/2"), ("POST", "/issues/2/comments")])
        one, two = self.fake.issues()
        self.assertEqual((one["state"], two["state"]), ("closed", "open"))
        self.assertTrue(two["comments"][-1].startswith("Flapping: 1 flaps"))
        self.assertEqual(self.outbox(pending_only=True), [])
        self.assertEqual([r["ticket"] for r in self.rows("SELECT ticket FROM incidents "
                                                         "ORDER BY id")], ["1", "2"])
        n = len(self.fake.calls)
        self.m().once()
        self.assertEqual(self.fake.calls[n:], [])

    def test_down_mid_replay_resumes_in_order(self):
        a = self.pull()
        self.now += 4
        self.resolve(a)
        self.fake.fail_next = [503]  # the tracker fails the create: outcome unknown
        self.m().once()
        self.assertEqual(len(self.fake.issues()), 0)
        self.m().once()
        issue = self.fake.issues()[0]
        self.assertEqual((issue["state"], len(issue["comments"])), ("closed", 1))

    def test_refused_op_kept_and_does_not_block_other_incidents(self):
        a = self.pull()
        self.m().once()
        self.now += 4
        self.resolve(a)
        self.m().plan()  # queued: a's comment and close
        b = self.pull()
        self.fake.fail_next = [404]  # the comment on a's issue is refused
        self.m().once()
        self.assertEqual(len(self.fake.issues()), 2)  # b's create still went out
        pend = self.outbox(pending_only=True)
        self.assertEqual([(o["key"], o["op"]) for o in pend],
                         [("lw-%d" % a, "comment"), ("lw-%d" % a, "close")])
        self.assertIsNone(pend[0]["sent"])
        self.m().once()
        self.assertEqual(self.fake.issues()[0]["state"], "closed")
        self.assertEqual(len(self.fake.issues()[0]["comments"]), 1)
        self.assertEqual(b, a + 1)


if __name__ == "__main__":
    unittest.main()
