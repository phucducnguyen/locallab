"""Lane Q: /ask questions and answers kept in asks.db for the dashboard."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

from bench import commands
from stub import make_db
from tests.test_q_commands import BENCH_TOKEN, CHAT, TOKEN, FakeServer

USER = 4242424
CHAT_DIGITS = CHAT.lstrip("-")


class AsksDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.secrets = os.path.join(d, "secrets")
        os.makedirs(self.secrets)
        for name, v in (("telegram_token", TOKEN), ("telegram_chat", CHAT),
                        ("bench_token", BENCH_TOKEN)):
            with open(os.path.join(self.secrets, name), "w") as f:
                f.write(v + "\n")
        self.db = os.path.join(d, "bench.db")
        make_db.build(self.db)
        self.asks_db = os.path.join(d, "asks.db")
        self.fake = FakeServer()
        self.out = io.StringIO()
        self.next_update = 1
        self._env = os.environ.pop("LW_ASKS_DB", None)

    def tearDown(self):
        if self._env is not None:
            os.environ["LW_ASKS_DB"] = self._env
        self.fake.close()
        self.tmp.cleanup()

    def bot(self, **kw):
        kw.setdefault("out", self.out)
        return commands.Bot(self.fake.base, self.secrets, self.db, self.fake.base,
                            os.path.join(self.tmp.name, "state"), poll_timeout=0, **kw)

    def agent(self, body):
        script = os.path.join(self.tmp.name, "agent.py")
        with open(script, "w") as f:
            f.write("import sys\n" + body)
        cfg = os.path.join(self.tmp.name, "agent.json")
        with open(cfg, "w") as f:
            json.dump({"agent_cmd": [sys.executable, script, "-m", "{message}"],
                       "timeout_s": 55}, f)
        return cfg

    def run_one(self, text, bot):
        uid = self.next_update
        self.next_update += 1
        self.fake.updates.append({"update_id": uid, "message": {
            "message_id": 500 + uid, "date": int(time.time()), "text": text,
            "chat": {"id": int(CHAT)}, "from": {"id": USER, "is_bot": False,
                                                 "username": "alice_bench",
                                                 "first_name": "Alice"}}})
        bot.step()
        bot.asks.join()

    def rows(self, path=None):
        conn = sqlite3.connect(path or self.asks_db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM asks ORDER BY id")]
        finally:
            conn.close()

    def replies(self):
        return [s["text"] for s in self.fake.sent]

    def test_default_path_next_to_bench_db_and_schema(self):
        bot = self.bot()
        self.assertEqual(bot.asks_db, self.asks_db)
        self.run_one("/ask hi", bot=self.bot(agent_config=self.agent("print('fine')\n")))
        conn = sqlite3.connect(self.asks_db)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            cols = [r[1] for r in conn.execute("PRAGMA table_info(asks)")]
        finally:
            conn.close()
        self.assertEqual(cols, ["id", "t_asked", "question", "incident_id", "answer",
                                "t_answered", "status"])

    def test_env_overrides_path(self):
        other = os.path.join(self.tmp.name, "elsewhere", "a.db")
        os.makedirs(os.path.dirname(other))
        os.environ["LW_ASKS_DB"] = other
        try:
            bot = self.bot(agent_config=self.agent("print('fine')\n"))
        finally:
            del os.environ["LW_ASKS_DB"]
        self.assertEqual(bot.asks_db, other)
        self.run_one("/ask hi", bot=bot)
        self.assertEqual(len(self.rows(other)), 1)
        self.assertFalse(os.path.exists(self.asks_db))

    def test_queued_row_then_answered(self):
        bot = self.bot(agent_config=self.agent("print('**Sensor** is stuck [event 4].')\n"))
        t0 = time.time()
        bot.cmd_ask(501, "why   is it stuck?")
        r = self.rows()
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["question"], "why is it stuck?")
        self.assertIsNone(r[0]["incident_id"])
        self.assertGreaterEqual(r[0]["t_asked"], t0 - 1)
        bot.asks.join()
        r = self.rows()[0]
        self.assertEqual(r["status"], "answered")
        self.assertEqual(r["answer"], "Sensor is stuck [event 4].")
        self.assertEqual(self.replies(), [r["answer"]])
        self.assertGreaterEqual(r["t_answered"], r["t_asked"])

    def test_asked_status_before_answer(self):
        bot = self.bot()
        row_id = bot.record_asked("q?", None)
        r = self.rows()[0]
        self.assertEqual((r["id"], r["status"], r["answer"], r["t_answered"]),
                         (row_id, "asked", None, None))

    def test_ask_incident_short_form(self):
        bot = self.bot(agent_config=self.agent("print('the code')\n"))
        self.run_one("/ask 5", bot=bot)
        r = self.rows()[0]
        self.assertEqual(r["question"], "/ask 5")
        self.assertEqual(r["incident_id"], 5)
        self.assertEqual(r["status"], "answered")

    def test_timeout_row(self):
        bot = self.bot(agent_config=self.agent("import time\ntime.sleep(30)\n"), ask_timeout=0.5)
        self.run_one("/ask anything", bot=bot)
        r = self.rows()[0]
        self.assertEqual(r["status"], "timeout")
        self.assertEqual(r["answer"], "ask: no answer within 0 s")
        self.assertEqual(self.replies(), [r["answer"]])

    def test_failed_rows(self):
        bot = self.bot(agent_config=self.agent("print('broke')\nsys.exit(3)\n"))
        self.run_one("/ask fail please", bot=bot)
        r = self.rows()[-1]
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["answer"], "ask: the agent failed (exit 3)\nbroke")
        self.assertEqual(self.replies()[-1], r["answer"])

    def test_failed_on_exception(self):
        bot = self.bot()

        def boom(question):
            raise RuntimeError("x")
        bot._ask = boom
        bot.cmd_ask(501, "q")
        bot.asks.join()
        r = self.rows()[0]
        self.assertEqual((r["status"], r["answer"]), ("failed", "ask: failed (RuntimeError)"))

    def test_db_errors_do_not_break_reply(self):
        bad = os.path.join(self.tmp.name, "no-such-dir", "asks.db")
        bot = self.bot(agent_config=self.agent("print('still here')\n"), asks_db=bad)
        self.run_one("/ask hi", bot=bot)
        self.assertEqual(self.replies(), ["still here"])
        logs = [ln for ln in self.out.getvalue().splitlines() if "asks DB" in ln]
        self.assertEqual(logs, ["commands: asks DB not written (OperationalError)"])

    def test_db_error_on_update_does_not_break_reply(self):
        bot = self.bot(agent_config=self.agent("print('still here')\n"))
        bot.cmd_ask(501, "hi")
        os.remove(self.asks_db)
        os.makedirs(self.asks_db)  # now a directory: the update fails
        bot.asks.join()
        self.assertEqual(self.replies(), ["still here"])
        self.assertIn("commands: asks DB not written", self.out.getvalue())

    def test_no_user_or_chat_ids_or_tokens(self):
        bot = self.bot(agent_config=self.agent("print('ok')\n"))
        self.run_one("/ask who am I?", bot=bot)
        self.run_one("/ask 7", bot=bot)
        for path in (self.asks_db, self.asks_db + "-wal"):
            if os.path.exists(path):
                with open(path, "rb") as f:
                    raw = f.read()
                for s in (str(USER), CHAT_DIGITS, "alice", "Alice", TOKEN, BENCH_TOKEN):
                    self.assertNotIn(s.encode(), raw)
        for r in self.rows():
            vals = " ".join(str(v) for v in r.values())
            self.assertNotIn(str(USER), vals)
            self.assertNotIn(CHAT_DIGITS, vals)
            self.assertNotIn("alice", vals.lower())


if __name__ == "__main__":
    unittest.main()
