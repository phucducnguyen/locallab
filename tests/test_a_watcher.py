"""Lane A stage A5: the repo watcher on a temp git repo, and the watcher-health posts
of bench.sender (blind, link down, link back) against a fake Telegram on 127.0.0.1.
"""

import io
import os
import tempfile
import threading
import time
import unittest

from bench import requests, store
from bench.sender import Sender, Telegram
from bench.watcher import Watcher
from tests.test_a_endpoint import git
from tests.test_a_sender import FAKE_CHAT, FAKE_TOKEN, FakeTelegram


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "bench-fw")
        os.makedirs(os.path.join(self.repo, "src"))
        git(self.repo, "init", "-q", "-b", "main")
        self.commit("one")
        self.db = os.path.join(self.tmp.name, "bench.db")
        self.conn = store.open_db(self.db)
        self.addCleanup(self.conn.close)
        self.logs = []

    def commit(self, text):
        with open(os.path.join(self.repo, "src", "a.c"), "w") as f:
            f.write("/* %s */\n" % text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", text)
        return git(self.repo, "rev-parse", "HEAD")

    def watcher(self):
        return Watcher(self.conn, self.repo, log=self.logs.append)

    def queued(self):
        return [(r["source"], r["kind"], r["arg"]) for r in self.conn.execute(
            "SELECT * FROM bench_requests ORDER BY id")]

    def test_new_commit_and_move_back_each_trigger_a_run(self):
        w = self.watcher()
        self.assertIsNone(w.poll())                 # first start: record, trigger nothing
        self.assertIsNone(w.poll())
        c2 = self.commit("two")
        self.assertIsNotNone(w.poll())
        self.assertIsNone(w.poll())                 # same head: nothing more
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")   # a move back
        c1 = git(self.repo, "rev-parse", "HEAD")
        w.poll()
        self.assertEqual(self.queued(), [("watcher", "run", c2), ("watcher", "run", c1)])

    def test_a_move_to_a_keyed_commit_queues_nothing(self):
        from tests.test_a_endpoint import make_history_repo
        repo = os.path.join(self.tmp.name, "history")
        k1, k2, k3 = make_history_repo(repo)
        w = Watcher(self.conn, repo, log=self.logs.append)
        w.poll()
        git(repo, "reset", "-q", "--hard", k1)        # main moved back into the keyed history
        self.assertIsNone(w.poll())
        self.assertIsNone(w.poll())
        self.assertEqual(len([s for s in self.logs if "not a bench commit" in s]), 1)
        git(repo, "reset", "-q", "--hard", k2)
        self.assertIsNotNone(w.poll())
        self.assertEqual(self.queued(), [("watcher", "run", k2)])

    def test_restart_triggers_only_on_a_real_change(self):
        self.watcher().poll()
        self.assertIsNone(self.watcher().poll())    # restarted, head unchanged
        c2 = self.commit("two")                     # changed while the watcher was down
        self.watcher().poll()
        self.assertEqual(self.queued(), [("watcher", "run", c2)])

    def test_unreadable_repo_logs_once_and_recovers(self):
        w = Watcher(self.conn, os.path.join(self.tmp.name, "nope"), log=self.logs.append)
        self.assertIsNone(w.poll())
        self.assertIsNone(w.poll())
        self.assertEqual(len([s for s in self.logs if "cannot read" in s]), 1)
        self.assertEqual(self.queued(), [])

    def test_loop_polls_every_interval(self):
        w = self.watcher()
        w.poll()
        stop = threading.Event()
        errors = []

        def run():  # its own connection, in its own thread, like the real process
            conn = store.connect(self.db)
            Watcher(conn, self.repo, log=errors.append).loop(0.1, stop)
            conn.close()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        c2 = self.commit("two")
        deadline = time.time() + 5
        while not self.queued() and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        th.join(2)
        self.assertEqual([e for e in errors if "failed" in e], [])
        self.assertEqual(self.queued(), [("watcher", "run", c2)])
        self.assertEqual(requests.busy(self.conn)["arg"], c2)


class WatcherHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.tok = os.path.join(d, "telegram_token")
        self.chat = os.path.join(d, "telegram_chat")
        for p, v in ((self.tok, FAKE_TOKEN), (self.chat, FAKE_CHAT)):
            with open(p, "w") as f:
                f.write(v + "\n")
        self.conn = store.open_db(os.path.join(d, "bench.db"))
        self.addCleanup(self.conn.close)
        self.tg = FakeTelegram()
        self.addCleanup(self.tg.close)
        self.now = [1000.0]
        self.out = io.StringIO()

    def sender(self):
        return Sender(self.conn, Telegram(self.tg.base, self.tok, self.chat, timeout=2),
                      out=self.out, clock=lambda: self.now[0])

    def texts(self):
        return [b["text"] for _, b in self.tg.requests]

    def link(self, kind, t):
        with self.conn:
            store.event(self.conn, t, "nucleo-1", kind, {"what": kind})

    def step_at(self, s, t):
        self.now[0] = t
        store.heartbeat(self.conn, t)
        s.step()

    def test_blind_once_with_the_time(self):
        store.heartbeat(self.conn, 1000.0)
        s = self.sender()
        for t in (1005.0, 1011.0, 1030.0):
            self.now[0] = t
            s.step()
        self.assertEqual(self.texts(), ["⚠️ Local Lab is blind: bench down since 00:16:40.000 UTC"])

    def test_short_drop_posts_nothing(self):
        s = self.sender()
        self.link("LINK_DOWN", 1000.0)
        self.step_at(s, 1005.0)
        self.link("LINK_UP", 1006.0)
        self.step_at(s, 1020.0)
        self.assertEqual(self.texts(), [])

    def test_link_down_10s_once_then_back_after_n_s(self):
        s = self.sender()
        self.link("LINK_DOWN", 1000.0)
        self.step_at(s, 1009.0)
        self.assertEqual(self.texts(), [])
        for t in (1010.0, 1012.0, 1015.0):
            self.step_at(s, t)
        self.assertEqual(self.texts(), [
            "⚠️ Local Lab is not watching bench-1: link down since 00:16:40.000 UTC"])
        self.link("LINK_UP", 1020.4)
        self.step_at(s, 1021.0)
        self.step_at(s, 1022.0)
        s2 = self.sender()                          # a restarted sender says nothing again
        self.step_at(s2, 1023.0)
        self.assertEqual(self.texts()[1:], ["⚠️ Local Lab: bench-1 link back after 20 s"])
        self.assertEqual(self.out.getvalue().count("WATCHER ⚠️ Local Lab: bench-1 link back"), 1)

    def test_board_line_after_restart_counts_as_link_back(self):
        s = self.sender()
        self.link("LINK_DOWN", 1000.0)
        self.step_at(s, 1004.0)
        self.link("HEALTH", 1006.0)                 # short drop, bench restarted: no LINK_UP
        self.step_at(s, 1030.0)
        self.assertEqual(self.texts(), [])
        self.link("LINK_DOWN", 1040.0)
        self.step_at(s, 1051.0)
        self.link("ALIVE", 1055.0)                  # long drop, then a board line
        self.step_at(s, 1056.0)
        self.assertEqual(self.texts(), [
            "⚠️ Local Lab is not watching bench-1: link down since 00:17:20.000 UTC",
            "⚠️ Local Lab: bench-1 link back after 15 s"])

    def test_old_outage_is_history(self):
        self.link("LINK_DOWN", 100.0)
        self.link("LINK_UP", 200.0)
        self.step_at(self.sender(), 1000.0)
        self.assertEqual(self.texts(), [])


if __name__ == "__main__":
    unittest.main()
