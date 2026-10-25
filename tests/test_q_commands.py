"""Lane Q: Telegram commands (bench/commands.py) against fake servers on 127.0.0.1."""

import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest

from bench import commands
from stub import make_db
from stub.db import Writer, connect

TOKEN = "123456:FAKE-test-token-QQQ"
CHAT = "-1001234"
BENCH_TOKEN = "fake-bench-token-QQQ"
SHA = "a" * 40


class FakeServer:
    """One HTTP server playing Telegram (getUpdates, sendMessage) and the bench endpoint."""

    def __init__(self):
        self.updates = []
        self.sent = []
        self.posts = []
        self.paths = []
        self.endpoint_status = 202
        self.endpoint_body = None
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.paths.append(self.path)
                if self.path.endswith("/getUpdates"):
                    off = body.get("offset") or 0
                    res = [u for u in outer.updates if u["update_id"] >= off]
                    data = {"ok": True, "result": res}
                    status = 200
                elif self.path.endswith("/sendMessage"):
                    outer.sent.append(body)
                    data = {"ok": True, "result": {"message_id": 1000 + len(outer.sent)}}
                    status = 200
                else:
                    outer.posts.append((self.path, body, self.headers.get("Authorization")))
                    status = outer.endpoint_status
                    data = outer.endpoint_body or {"accepted": True, "request_id": 7}
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class CommandsTest(unittest.TestCase):
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
        conn = connect(self.db)
        w = Writer(conn)
        self.now = time.time()
        w.serial(self.now - 5, "LW HEALTH 99 loopback=OK sensor=STUCK radio=OK estop=OFF "
                 "vdd=3290 temp=31", "HEALTH", n=99, loopback="OK", sensor="STUCK",
                 radio="OK", estop="OFF", vdd=3290, temp=31)
        conn.execute("CREATE TABLE bench_state (board TEXT PRIMARY KEY, verified_sha TEXT, "
                     "t REAL NOT NULL)")
        conn.execute("INSERT INTO bench_state VALUES ('nucleo-1', ?, ?)", (SHA, self.now))
        conn.commit()
        conn.close()
        self.state = os.path.join(d, "state")
        self.fake = FakeServer()
        self.out = io.StringIO()
        self.next_update = 1

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def bot(self, **kw):
        kw.setdefault("out", self.out)
        return commands.Bot(self.fake.base, self.secrets, self.db, self.fake.base, self.state,
                            poll_timeout=0, **kw)

    def msg(self, text, chat=CHAT, user=42):
        uid = self.next_update
        self.next_update += 1
        self.fake.updates.append({"update_id": uid, "message": {
            "message_id": 500 + uid, "date": int(time.time()), "text": text,
            "chat": {"id": int(chat)}, "from": {"id": user, "is_bot": False}}})
        return uid

    def replies(self):
        return [s["text"] for s in self.fake.sent]

    def run_one(self, text, bot=None, **kw):
        bot = bot or self.bot()
        self.msg(text, **kw)
        bot.step()
        bot.asks.join()
        return bot

    # ---------- filtering ----------

    def test_wrong_chat_ignored_without_text(self):
        self.run_one("/status secret words", chat="-999")
        self.assertEqual(self.fake.sent, [])
        self.assertIn("ignored update 1 from chat -999", self.out.getvalue())
        self.assertNotIn("secret words", self.out.getvalue())

    def test_non_command_ignored(self):
        self.run_one("hello there")
        self.assertEqual(self.fake.sent, [])
        self.assertIn("ignored update 1 from chat %s" % CHAT, self.out.getvalue())
        self.assertNotIn("hello there", self.out.getvalue())

    def test_user_allowlist(self):
        bot = self.bot(users={"7"})
        self.run_one("/help", bot=bot, user=42)
        self.assertEqual(self.fake.sent, [])
        self.run_one("/help", bot=bot, user=7)
        self.assertEqual(len(self.fake.sent), 1)

    def test_help_unknown_and_reply_to(self):
        bot = self.bot()
        self.run_one("/help", bot=bot, user=1)
        self.run_one("/frobnicate", bot=bot, user=2)
        self.assertIn("/test <name>", self.replies()[0])
        self.assertEqual(self.replies()[1], "unknown command; /help")
        self.assertEqual(self.fake.sent[0]["reply_parameters"]["message_id"], 501)
        self.assertEqual(self.fake.sent[0]["chat_id"], CHAT)

    # ---------- /status ----------

    def test_status_content(self):
        self.run_one("/status")
        text = self.replies()[0]
        self.assertIn("loopback OK", text)
        self.assertIn("sensor STUCK", text)
        self.assertIn("estop OFF", text)
        self.assertIn("vdd 3290 mV", text)
        self.assertIn("temp 31 C", text)
        self.assertIn("verified sha: aaaaaaaaaaaa", text)
        self.assertIn("#3 HARDFAULT nucleo-1 CODE OPEN", text)
        self.assertIn("last run 2: 2222222 HARDFAULT", text)
        self.assertIn("HARDFAULT", text)
        self.assertLessEqual(len(text.splitlines()), 10)
        self.assertEqual(self.fake.posts, [])

    # ---------- /test ----------

    def test_test_valid_queued(self):
        self.run_one("/test crc32")
        self.assertEqual(self.fake.posts, [("/board/test", {"name": "crc32"},
                                            "Bearer " + BENCH_TOKEN)])
        self.assertEqual(self.replies(), ["queued: test crc32 (request 7)"])

    def test_test_invalid_name_never_posts(self):
        self.run_one("/test rm-rf")
        self.assertEqual(self.fake.posts, [])
        self.assertIn("usage: /test <name>", self.replies()[0])

    def test_test_429_reason(self):
        self.fake.endpoint_status = 429
        self.fake.endpoint_body = {"error": "board busy: a run in progress"}
        self.run_one("/test loopback")
        self.assertEqual(self.replies(), ["not queued (429): board busy: a run in progress"])

    def test_test_400_reason(self):
        self.fake.endpoint_status = 400
        self.fake.endpoint_body = {"error": "name must be one of the five self-tests"}
        self.run_one("/test systick")
        self.assertIn("not queued (400)", self.replies()[0])

    # ---------- /rerun ----------

    def test_rerun_posts_verified_sha(self):
        self.run_one("/rerun")
        self.assertEqual(self.fake.posts[0][:2], ("/run", {"sha": SHA}))
        self.assertEqual(self.replies(), ["queued: run aaaaaaaaaaaa (request 7)"])

    def test_rerun_429(self):
        self.fake.endpoint_status = 429
        self.fake.endpoint_body = {"error": "one run per 60 s", "retry_after_s": 41.5}
        self.run_one("/rerun")
        self.assertEqual(self.replies(), ["not queued (429): one run per 60 s, retry in 41.5 s"])

    # ---------- /ask ----------

    def agent(self, body):
        script = os.path.join(self.tmp.name, "agent.py")
        with open(script, "w") as f:
            f.write("import sys, json\n"
                    "json.dump(sys.argv, open(%r, 'w'))\n" % os.path.join(self.tmp.name, "argv")
                    + body)
        cfg = os.path.join(self.tmp.name, "agent.json")
        with open(cfg, "w") as f:
            json.dump({"agent_cmd": [sys.executable, script, "--session", "{session}",
                                     "-m", "{message}"], "timeout_s": 55}, f)
        return cfg

    def test_ask_answer_condensed(self):
        lines = ["## Answer", "", "**The sensor is STUCK** since HEALTH 99 [event 40].",
                 "- loopback `OK` _cmd_dispatch_ HEALTH_FAIL"] + ["line %d [event %d]" % (i, i) for i in range(20)]
        cfg = self.agent("print(%r)\n" % "\n".join(lines))
        self.run_one("/ask why is the sensor   stuck?\x07", bot=self.bot(agent_config=cfg))
        text = self.replies()[0]
        out = text.splitlines()
        self.assertLessEqual(len(out), 8)
        self.assertEqual(out[0], "Answer")
        self.assertEqual(out[1], "The sensor is STUCK since HEALTH 99 [event 40].")
        self.assertNotIn("*", text)
        self.assertNotIn("`", text)
        self.assertEqual(out[2], "- loopback OK cmd_dispatch HEALTH_FAIL")
        with open(os.path.join(self.tmp.name, "argv")) as f:
            argv = json.load(f)
        self.assertTrue(argv[2].startswith("lw-ask-"))
        self.assertIn("I can't tell", argv[4])
        self.assertTrue(argv[4].endswith('"why is the sensor stuck?"'))

    def test_ask_question_trimmed(self):
        cfg = self.agent("print('ok')\n")
        self.run_one("/ask " + "x" * 1000, bot=self.bot(agent_config=cfg))
        with open(os.path.join(self.tmp.name, "argv")) as f:
            msg = json.load(f)[4]
        self.assertEqual(msg.count("x"), 300)

    def test_ask_timeout(self):
        cfg = self.agent("import time\ntime.sleep(30)\n")
        t0 = time.monotonic()
        self.run_one("/ask anything", bot=self.bot(agent_config=cfg, ask_timeout=0.5))
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(self.replies(), ["ask: no answer within 0 s"])

    def test_ask_busy_beyond_queue(self):
        cfg = self.agent("import time\ntime.sleep(0.6)\nprint('done')\n")
        bot = self.bot(agent_config=cfg)
        for u in range(6):
            self.msg("/ask q%d" % u, user=100 + u)
        bot.step()
        bot.asks.join()
        texts = self.replies()
        self.assertGreaterEqual(texts.count("busy: 3 questions already waiting"), 1)
        self.assertGreaterEqual(texts.count("done"), 3)
        self.assertEqual(len(texts), 6)

    # ---------- offset, rate limit, secrets ----------

    def test_offset_persisted_no_replay(self):
        self.run_one("/test crc32")
        self.assertEqual(len(self.fake.posts), 1)
        bot2 = self.bot()          # a restart: a new process reads the offset file
        bot2.step()
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(len(self.fake.sent), 1)
        with open(os.path.join(self.state, "telegram_offset")) as f:
            self.assertEqual(f.read().strip(), "2")
        self.msg("/help", user=9)
        bot2.step()
        self.assertEqual(len(self.fake.sent), 2)

    def test_stale_command_skipped(self):
        self.msg("/rerun")
        self.fake.updates[-1]["message"]["date"] = int(time.time()) - 3600
        self.bot().step()
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.fake.sent, [])

    def test_rate_limit(self):
        clock = [1000.0]
        bot = self.bot(clock=lambda: clock[0])
        for _ in range(4):
            self.msg("/help")
        for u in self.fake.updates:
            u["message"]["date"] = 1000
        bot.step()
        self.assertEqual(len(self.replies()), 2)
        self.assertTrue(self.replies()[1].startswith("slow down"))
        clock[0] += 3.5
        self.msg("/help")
        self.fake.updates[-1]["message"]["date"] = int(clock[0])
        bot.step()
        self.assertEqual(len(self.replies()), 3)
        self.assertIn("/status", self.replies()[2])

    def test_token_never_in_output_or_argv(self):
        cfg = self.agent("print('fine')\n")
        bot = self.bot(agent_config=cfg)
        for i, t in enumerate(("/status", "/test crc32", "/rerun", "/ask hi", "/zz", "nope")):
            self.msg(t, user=200 + i)
        self.msg("/status", chat="-5")
        bot.step()
        bot.asks.join()
        with open(os.path.join(self.tmp.name, "argv")) as f:
            argv = f.read()
        dry = io.StringIO()
        dbot = self.bot(dry_run=True, out=dry)
        self.msg("/help", user=300)
        dbot.step()
        everything = self.out.getvalue() + dry.getvalue() + argv + json.dumps(self.fake.sent)
        for secret in (TOKEN, BENCH_TOKEN):
            self.assertNotIn(secret, everything)
        self.assertIn("would reply to", dry.getvalue())
        self.assertEqual(len(self.fake.sent), 5)  # "nope" and the dry run sent nothing

    def test_unreachable_telegram_error_has_no_token(self):
        bot = commands.Bot("http://127.0.0.1:9", self.secrets, self.db, self.fake.base,
                           self.state, out=self.out, poll_timeout=0)
        with self.assertRaises(commands.SendError) as cm:
            bot.step()
        self.assertNotIn(TOKEN, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
