"""Lane A: bench.sender against a fake Telegram server on 127.0.0.1.

Fake token and chat files in a temp dir; no network beyond loopback.
"""

import http.server
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest

from bench import parser, store
from bench.run import Bench
from bench.sender import Sender, Telegram

FAKE_TOKEN = "123456:FAKE-token-for-tests-only"
FAKE_CHAT = "-1001234"


class FakeTelegram:
    def __init__(self, port=0):
        self.requests = []
        self.times = []
        self.next_id = 100
        self.fail_with = None
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((self.path, json.loads(body)))
                outer.times.append(time.time())
                if outer.fail_with:
                    self.send_response(outer.fail_with)
                    self.end_headers()
                    return
                outer.next_id += 1
                data = json.dumps({"ok": True, "result": {"message_id": outer.next_id}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append((self.path, None))
                self.send_response(404)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.token = os.path.join(d, "telegram_token")
        self.chat = os.path.join(d, "telegram_chat")
        with open(self.token, "w") as f:
            f.write(FAKE_TOKEN + "\n")
        with open(self.chat, "w") as f:
            f.write(FAKE_CHAT + "\n")
        os.chmod(self.token, 0o600)
        self.db = os.path.join(d, "bench.db")
        self.conn = store.open_db(self.db)
        self.out = io.StringIO()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def sender(self, base, clock=time.time):
        conn = store.connect(self.db)
        if threading.current_thread() is threading.main_thread():
            self.addCleanup(conn.close)
        return Sender(conn, Telegram(base, self.token, self.chat, timeout=2),
                      out=self.out, clock=clock)

    def open_incident(self, t0=None):
        """Through the bench: two bad HEALTH lines -> incident + open alert in the outbox."""
        b = Bench(self.conn, "nucleo-1", log=lambda s: None)
        t0 = time.time() if t0 is None else t0
        b.apply(b.m.link_up(t0), t0)
        for i, t in enumerate((t0, t0 + 0.001)):
            b.line(t, "x", parser.parse_line("LW HEALTH %d loopback=OPEN" % (i + 1))["records"])
        return b, self.conn.execute("SELECT * FROM incidents").fetchone()

    def alerts(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM alerts ORDER BY id")]


class TestSender(Base):
    def setUp(self):
        Base.setUp(self)
        self.tg = FakeTelegram()

    def tearDown(self):
        self.tg.close()
        Base.tearDown(self)

    def test_send_store_id_and_never_resend(self):
        self.open_incident()
        s = self.sender(self.tg.base)
        self.assertEqual(s.step(), 1)
        path, body = self.tg.requests[0]
        self.assertEqual(path, "/bot%s/sendMessage" % FAKE_TOKEN)
        self.assertEqual(body["chat_id"], FAKE_CHAT)
        self.assertTrue(body["text"].startswith("LAB WATCH incident 1 OPEN"))
        self.assertNotIn("parse_mode", body)
        a = self.alerts()[0]
        self.assertEqual(a["telegram_message_id"], 101)
        self.assertIsNotNone(a["sent"])
        inc = self.conn.execute("SELECT t_alert_sent FROM incidents").fetchone()
        self.assertEqual(inc["t_alert_sent"], a["sent"])
        sent_ev = self.conn.execute("SELECT * FROM events WHERE kind='ALERT_SENT'").fetchall()
        self.assertEqual(len(sent_ev), 1)
        self.assertEqual(s.step(), 0)
        s2 = self.sender(self.tg.base)          # a restarted sender
        self.assertEqual(s2.step(), 0)
        self.assertEqual(len(self.tg.requests), 1)
        out = self.out.getvalue()
        self.assertEqual(out.count("LAB WATCH incident 1 OPEN"), 1)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn("FAKE-token", out)

    def test_reply_waits_for_parent_and_uses_its_message_id(self):
        b, inc = self.open_incident()
        store.add_alert(self.conn, inc["id"], "explain", "LAB WATCH incident 1 explanation", 1.0)
        self.conn.commit()
        self.tg.fail_with = 502
        s = self.sender(self.tg.base)
        s.step()
        self.assertEqual(len(self.tg.requests), 1)   # the reply did not jump ahead
        self.assertTrue(all(a["sent"] is None for a in self.alerts()))
        self.tg.fail_with = None
        s._retry.clear()
        s.step()
        s.step()
        a = self.alerts()
        self.assertTrue(all(x["sent"] is not None for x in a))
        reply_body = self.tg.requests[-1][1]
        self.assertEqual(reply_body["reply_parameters"]["message_id"], a[0]["telegram_message_id"])
        self.assertEqual(a[1]["reply_to_alert_id"], a[0]["id"])

    def test_open_to_send_under_5s_with_sender_loop(self):
        stop = threading.Event()

        def run():
            s = self.sender(self.tg.base)   # its own connection, in its own thread
            s.loop(0.2, stop)
            s.conn.close()

        th = threading.Thread(target=run, daemon=True)
        th.start()
        self.addCleanup(th.join, 2)
        self.addCleanup(stop.set)
        time.sleep(0.3)
        t_first_bad = time.time()
        _, inc = self.open_incident(t0=t_first_bad)
        t_open = inc["t_open"]
        deadline = time.time() + 5
        while not self.tg.times and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.tg.times, "nothing reached the fake Telegram")
        open_to_send = self.tg.times[0] - t_open
        self.assertLess(open_to_send, 1.0)
        time.sleep(0.3)
        row = self.conn.execute("SELECT t_first_bad_line, t_open, t_alert_sent FROM "
                                "incidents").fetchone()
        self.assertLess(row["t_alert_sent"] - row["t_first_bad_line"], 5.0)
        self.assertTrue(all(p.endswith("/sendMessage") for p, _ in self.tg.requests))
        self.assertFalse(any("getUpdates" in p for p, _ in self.tg.requests))

    def test_blind_post_once_per_outage(self):
        store.heartbeat(self.conn, 1000.0)
        now = [1005.0]
        s = self.sender(self.tg.base, clock=lambda: now[0])
        s.step()
        self.assertEqual(self.tg.requests, [])
        now[0] = 1011.0
        s.step()
        now[0] = 1020.0
        s.step()
        texts = [b["text"] for _, b in self.tg.requests]
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("Lab Watch is blind: bench down since "))
        store.heartbeat(self.conn, 1030.0)   # the bench came back, then died again
        now[0] = 1050.0
        s.step()
        self.assertEqual(len(self.tg.requests), 2)
        self.assertEqual(self.out.getvalue().count("WATCHER Lab Watch is blind"), 2)


class TestUnreachable(Base):
    def test_keeps_row_prints_once_and_sends_when_back(self):
        port = free_port()
        self.open_incident()
        s = self.sender("http://127.0.0.1:%d" % port)
        for _ in range(3):
            self.assertEqual(s.step(), 0)
            s._retry.clear()
        a = self.alerts()[0]
        self.assertIsNone(a["sent"])
        out = self.out.getvalue()
        self.assertEqual(out.count("LAB WATCH incident 1 OPEN"), 1)   # console has it already
        self.assertIn("unreachable", out)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn("127.0.0.1", out)
        tg = FakeTelegram(port=port)
        try:
            s._retry.clear()
            self.assertEqual(s.step(), 1)
            self.assertIsNotNone(self.alerts()[0]["sent"])
            self.assertEqual(len(tg.requests), 1)
            s2 = self.sender(tg.base)
            s2.step()
            self.assertEqual(len(tg.requests), 1)
            self.assertEqual(self.out.getvalue().count("LAB WATCH incident 1 OPEN"), 1)
        finally:
            tg.close()

    def test_missing_token_file_keeps_row(self):
        os.unlink(self.token)
        self.open_incident()
        s = self.sender("http://127.0.0.1:%d" % free_port())
        self.assertEqual(s.step(), 0)
        self.assertIsNone(self.alerts()[0]["sent"])
        self.assertIn("cannot be read", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
