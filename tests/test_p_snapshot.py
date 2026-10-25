"""Lane P: board photo replies (bench/snapshot.py) against a fake Telegram and fake screenshot."""

import email.parser
import email.policy
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bench import snapshot
from stub import make_db
from stub.db import Writer, connect

TOKEN = "123456:FAKE-token-for-tests-only"
TINY_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0bIDATx\x9cc\xf8\x0f"
            b"\x00\x01\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82")


class FakeTelegram:
    def __init__(self):
        self.photos = []
        self.paths = []
        self.fail = 0
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fake.paths.append(self.path)
                if fake.fail:
                    fake.fail -= 1
                    self.send_response(502)
                    self.end_headers()
                    return
                msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                parts = {}
                for part in msg.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    parts[name] = {"data": part.get_payload(decode=True),
                                   "filename": part.get_filename(),
                                   "type": part.get_content_type()}
                fake.photos.append(parts)
                out = json.dumps({"ok": True, "result": {"message_id": 9000 + len(fake.photos)}})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out.encode())

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.db = os.path.join(d, "bench.db")
        make_db.build(self.db)
        self.secrets = os.path.join(d, "secrets")
        os.mkdir(self.secrets)
        for name, val in (("telegram_token", TOKEN), ("telegram_chat", "-1001")):
            with open(os.path.join(self.secrets, name), "w") as f:
                f.write(val + "\n")
        self.state_dir = os.path.join(d, "state")
        self.fake = FakeTelegram()
        self.addCleanup(self.fake.close)
        self.shot = os.path.join(d, "shot.py")
        with open(self.shot, "w") as f:
            f.write("import sys\nopen(sys.argv[1], 'wb').write(%r)\nprint(sys.argv[2])\n"
                    % TINY_PNG)
        self.cmd = [sys.executable, self.shot, "{out}", "{url}"]
        conn = connect(self.db)
        self.last_sent = conn.execute("SELECT MAX(sent) FROM alerts").fetchone()[0]
        self.opens = [dict(r) for r in conn.execute(
            "SELECT a.id, a.incident_id, a.telegram_message_id, i.kind FROM alerts a "
            "JOIN incidents i ON i.id=a.incident_id WHERE a.transition='open' ORDER BY a.id")]
        conn.close()
        self.now = self.last_sent + 5.0

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, cmd=None, out=None, give_up=snapshot.GIVE_UP_S, startup_skip=10 ** 9):
        tg = snapshot.Telegram(self.fake.url, os.path.join(self.secrets, "telegram_token"),
                               os.path.join(self.secrets, "telegram_chat"), timeout=5)
        bench = snapshot.open_bench_ro(self.db)
        state = snapshot.open_state(self.state_dir)
        self.addCleanup(bench.close)
        self.addCleanup(state.close)
        self.out = out or io.StringIO()
        return snapshot.Snapshotter(bench, state, tg, cmd=cmd or self.cmd,
                                    url="http://127.0.0.1:8770/", out=self.out,
                                    clock=lambda: self.now, give_up=give_up,
                                    startup_skip=startup_skip, settle=0)


class TestSend(Base):
    def test_photo_reply_per_open_alert(self):
        s = self.make()
        s.step()
        self.assertEqual(len(self.fake.photos), len(self.opens))
        self.assertTrue(self.opens)
        for p, a in zip(self.fake.photos, self.opens):
            self.assertEqual(p["chat_id"]["data"], b"-1001")
            self.assertEqual(p["reply_to_message_id"]["data"],
                             str(a["telegram_message_id"]).encode())
            cap = p["caption"]["data"].decode()
            self.assertTrue(cap.endswith("(incident #%d)" % a["incident_id"]), cap)
            self.assertTrue(p["photo"]["data"].startswith(b"\x89PNG"))
            self.assertEqual(p["photo"]["type"], "image/png")
            self.assertTrue(p["photo"]["filename"].endswith(".png"))
        self.assertTrue(all(x.endswith("/sendPhoto") for x in self.fake.paths))
        first = self.fake.photos[0]["caption"]["data"].decode()
        self.assertEqual(first, "Board 1: the test wire (A0–A1) is disconnected "
                                "(incident #%d)" % self.opens[0]["incident_id"])

    def test_no_duplicates_across_restarts(self):
        self.make().step()
        n = len(self.fake.photos)
        s2 = self.make()
        s2.step()
        s2.step()
        self.assertEqual(len(self.fake.photos), n)

    def test_waits_for_red_state(self):
        self.now = self.last_sent + 0.5
        s = self.make()
        s.step()
        self.assertEqual(len(self.fake.photos), len(self.opens) - 1)
        self.now = self.last_sent + 2.0
        s.step()
        self.assertEqual(len(self.fake.photos), len(self.opens))

    def test_old_incidents_skipped_at_startup(self):
        self.now = self.last_sent + 3600
        s = self.make(startup_skip=snapshot.STARTUP_SKIP_S)
        s.step()
        self.assertEqual(self.fake.photos, [])
        self.assertIn("older than 10 min", self.out.getvalue())

    def test_link_down_skipped(self):
        conn = connect(self.db)
        w = Writer(conn)
        iid = w.open_incident(self.last_sent + 1, "LINK_DOWN", "HARDWARE", "no bytes",
                              {"lines": []})
        w.alert(self.last_sent + 1, iid, "open")
        conn.close()
        self.now = self.last_sent + 10
        s = self.make()
        s.step()
        caps = [p["caption"]["data"].decode() for p in self.fake.photos]
        self.assertFalse(any("#%d)" % iid in c for c in caps), caps)
        self.assertEqual(len(self.fake.photos), len(self.opens))
        self.assertIn("LINK_DOWN", self.out.getvalue())

    def test_screenshot_failure_no_send_logged(self):
        bad = [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"]
        s = self.make(cmd=bad)
        s.step()
        self.assertEqual(self.fake.photos, [])
        log = self.out.getvalue()
        self.assertIn("failed (screenshot wrote no file (exit 3: boom))", log)
        st = sqlite3.connect(os.path.join(self.state_dir, "snapshots.db"))
        self.assertEqual({r[0] for r in st.execute("SELECT status FROM photos")}, {"failed"})
        st.close()

    def test_not_png_is_failure(self):
        notpng = [sys.executable, "-c", "import sys; open(sys.argv[1],'w').write('hi')", "{out}"]
        s = self.make(cmd=notpng)
        s.step()
        self.assertEqual(self.fake.photos, [])
        self.assertIn("not a PNG", self.out.getvalue())

    def test_retry_then_send_and_give_up(self):
        self.fake.fail = 1
        s = self.make()
        s.step()
        self.assertIn("retry #1", self.out.getvalue())
        self.now += 60
        s.step()
        self.assertEqual(len(self.fake.photos), len(self.opens))

        self.fake.photos.clear()
        self.fake.fail = 10 ** 6
        self.tmp2 = os.path.join(self.tmp.name, "state2")
        self.state_dir = self.tmp2
        s = self.make(give_up=100)
        s.step()
        self.now += 200
        s.step()
        self.assertIn("gave up after 100 s", self.out.getvalue())
        self.assertEqual(self.fake.photos, [])

    def test_token_never_in_argv_or_output(self):
        argv_log = os.path.join(self.tmp.name, "argv.json")
        rec = [sys.executable, "-c",
               "import sys,json; json.dump(sys.argv, open(%r,'w')); open(sys.argv[1],'wb')"
               ".write(%r)" % (argv_log, TINY_PNG), "{out}", "{url}"]
        self.fake.fail = 1
        s = self.make(cmd=rec)
        s.step()
        self.now += 60
        s.step()
        with open(argv_log) as f:
            self.assertNotIn(TOKEN, f.read())
        self.assertNotIn(TOKEN, self.out.getvalue())
        self.assertNotIn("FAKE-token", self.out.getvalue())


class TestSettleProxy(unittest.TestCase):
    def test_injects_settle_image_and_forwards(self):
        class Dash(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = (b"<html><body>board</body></html>" if self.path == "/"
                        else b'{"ok": 1}' if self.path == "/state.json" else None)
                self.send_response(200 if body else 404)
                self.end_headers()
                self.wfile.write(body or b"")

        dash = ThreadingHTTPServer(("127.0.0.1", 0), Dash)
        threading.Thread(target=dash.serve_forever, daemon=True).start()
        self.addCleanup(dash.server_close)
        self.addCleanup(dash.shutdown)
        p = snapshot.SettleProxy("http://127.0.0.1:%d/" % dash.server_address[1], settle=0.2)
        self.addCleanup(p.close)
        import urllib.request
        page = urllib.request.urlopen(p.url).read()
        self.assertIn(b'<img src="__lw_settle.png"', page)
        self.assertIn(b"board", page)
        self.assertEqual(urllib.request.urlopen(p.url + "state.json").read(), b'{"ok": 1}')
        img = urllib.request.urlopen(p.url + "__lw_settle.png").read()
        self.assertTrue(img.startswith(b"\x89PNG"))

    def test_rejects_other_hosts_schemes_and_dotdot(self):
        import http.client
        p = snapshot.SettleProxy("http://127.0.0.1:9/", settle=0)
        self.addCleanup(p.close)
        port = p.srv.server_address[1]
        self.assertEqual(p.srv.server_address[0], "127.0.0.1")
        for target in ("//127.0.0.2/x", "http://other/", "file:///etc/passwd",
                       "/../etc/passwd", "/state.json/../x", "/etc/passwd"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", target)
            r = c.getresponse()
            r.read()
            c.close()
            self.assertIn(r.status, (400, 404), target)
        with self.assertRaises(ValueError):
            snapshot.SettleProxy("file:///etc/passwd")


class TestCaption(unittest.TestCase):
    def cap(self, kind, ev, board="nucleo-1", iid=5):
        return snapshot.caption({"id": iid, "kind": kind, "board": board,
                                 "evidence": json.dumps(ev)})

    def test_words(self):
        self.assertEqual(self.cap("HEALTH_FAIL", {"device": "sensor", "fields": {"sensor": "OPEN"}}),
                         "Board 1: the sensor wire (A2–A3) is disconnected (incident #5)")
        self.assertEqual(self.cap("HEALTH_FAIL", {"fields": {"radio": "STUCK"}}),
                         "Board 1: the radio wire (A4–A5) is shorted to ground (incident #5)")
        self.assertEqual(self.cap("ESTOP", {}),
                         "Board 1: the emergency stop button is pressed (incident #5)")
        self.assertEqual(self.cap("HEALTH_FAIL", {"device": "vdd"}),
                         "Board 1: the power supply is low (incident #5)")
        self.assertEqual(self.cap("HARDFAULT", {}, board="bench2"),
                         "bench2: the firmware crashed (incident #5)")
        self.assertEqual(self.cap("WEIRD_THING", {}),
                         "Board 1: there is a problem: weird thing (incident #5)")


class TestMain(unittest.TestCase):
    def test_bad_cmd_env(self):
        old = os.environ.get("LW_SNAPSHOT_CMD")
        os.environ["LW_SNAPSHOT_CMD"] = '{"not": "a list"}'
        try:
            with self.assertRaises(ValueError):
                snapshot.snapshot_cmd()
        finally:
            if old is None:
                del os.environ["LW_SNAPSHOT_CMD"]
            else:
                os.environ["LW_SNAPSHOT_CMD"] = old

    def test_default_cmd_has_own_profile(self):
        cmd = snapshot.DEFAULT_CMD
        self.assertIn("--no-remote", cmd)
        self.assertEqual(cmd[cmd.index("--profile") + 1], "{profile}")


if __name__ == "__main__":
    unittest.main()
