import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from stub import make_db, serve

TOKEN = "fake-token-for-tests"


class StubServeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "bench.db")
        make_db.build(cls.db)
        cls.hold = os.path.join(cls.tmp.name, "hold")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.audit = io.StringIO()
        self.srv = serve.make_server(self.db, "127.0.0.1", 0, TOKEN, self.hold, self.audit)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        if os.path.exists(self.hold):
            os.remove(self.hold)

    def req(self, path, method="GET", body=None, token=TOKEN, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            r.add_header("Authorization", "Bearer " + token)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                text = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            text, status = e.read(), e.code
            e.close()
        text.decode("ascii")  # printable ASCII only
        return status, json.loads(text)

    def test_health_needs_no_token_and_says_stub(self):
        status, body = self.req("/health", token=None)
        self.assertEqual(status, 200)
        self.assertIs(body["stub"], True)

    def test_token_required_everywhere_else(self):
        for path in ("/board", "/incidents", "/incidents/1", "/runs", "/runs/1", "/events",
                     "/show?ref=2222222"):
            self.assertEqual(self.req(path, token=None)[0], 401, path)
            self.assertEqual(self.req(path, token="wrong")[0], 401, path)
        self.assertEqual(self.req("/run", "POST", {"sha": "2222222"}, token=None)[0], 401)

    def test_incidents(self):
        status, body = self.req("/incidents")
        self.assertEqual(status, 200)
        self.assertEqual([i["id"] for i in body["incidents"]], [3, 2, 1])
        _, body = self.req("/incidents?open=1")
        self.assertEqual({i["id"] for i in body["incidents"]}, {2, 3})
        status, inc = self.req("/incidents/3")
        self.assertEqual(status, 200)
        self.assertEqual(inc["evidence"]["file_line"], "src/demo.c:42")
        self.assertEqual(inc["last_pass"]["sha"], "1111111")
        self.assertEqual(inc["routing"][0]["route"], "Firmware team")
        self.assertEqual(self.req("/incidents/99")[0], 404)

    def test_runs_mark_commit_text_as_data(self):
        _, body = self.req("/runs")
        self.assertEqual([r["run_id"] for r in body["runs"]], [2, 1])
        _, run = self.req("/runs/2")
        self.assertEqual(run["verdict"], "HARDFAULT")
        self.assertNotIn("subject", run)
        self.assertIn("subject", run["data"])
        self.assertEqual(self.req("/runs/99")[0], 404)

    def test_events_cursor(self):
        _, first = self.req("/events?since=0")
        ids = [e["id"] for e in first["events"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(first["cursor"], ids[-1])
        _, rest = self.req("/events?since=%d" % ids[5])
        self.assertEqual(rest["events"][0]["id"], ids[6])
        _, none = self.req("/events?since=%d" % first["cursor"])
        self.assertEqual((none["events"], none["cursor"]), ([], first["cursor"]))
        self.assertEqual(self.req("/events?since=abc")[0], 400)
        self.assertEqual(self.req("/events?since=-1")[0], 400)

    def test_board(self):
        _, body = self.req("/board")
        b = body["boards"][0]
        self.assertEqual(b["board"], "nucleo-1")
        self.assertEqual(b["boot"]["fw"], "2222222")
        self.assertEqual(b["open_incidents"], 2)

    def test_show_only_the_canned_commit(self):
        status, body = self.req("/show?ref=2222222")
        self.assertEqual(status, 200)
        self.assertTrue(any(line.startswith("+++ b/src/") for line in body["data"]["diff"]))
        self.assertLessEqual(len(body["data"]["diff"]), 400)
        for ref in ("main:CASES.md", "bug/example", "--output=x",  # the interface's 400 cases
                    "1111111", "", "222222", "2222222g", "HEAD", "2222222%0A", "2" * 41,
                    "%2D%2Doutput%3Dx"):
            self.assertEqual(self.req("/show?ref=" + ref)[0], 400, ref)
        self.assertEqual(self.req("/show")[0], 400)

    def test_post_run_rate_limit(self):
        self.assertEqual(self.req("/run", "POST", {"sha": "2222222"})[0], 202)
        status, body = self.req("/run", "POST", {"sha": "2222222"})
        self.assertEqual(status, 429)
        self.assertIn("AUDIT POST /run", self.audit.getvalue())
        self.assertNotIn(TOKEN, self.audit.getvalue())

    def test_post_run_validates_sha(self):
        for body in ({"sha": "main"}, {"sha": "--output=x"}, {}, ["2222222"]):
            self.assertEqual(self.req("/run", "POST", body)[0], 400, body)
        self.assertEqual(self.req("/run", "POST", raw=b"{not json")[0], 400)
        self.assertEqual(self.req("/run", "POST", raw=b"x" * 5000)[0], 400)

    def test_post_board_test(self):
        self.assertEqual(self.req("/board/test", "POST", {"name": "loopback"})[0], 202)
        self.assertEqual(self.req("/board/test", "POST", {"name": "selftest"})[0], 400)
        self.assertEqual(self.req("/board/test", "POST", {})[0], 400)

    def test_hold_file_refuses_side_effects(self):
        with open(self.hold, "w") as f:
            f.write("scorer 10:30\n")
        self.assertEqual(self.req("/run", "POST", {"sha": "2222222"})[0], 429)
        self.assertEqual(self.req("/board/test", "POST", {"name": "crc32"})[0], 429)
        self.assertEqual(self.req("/incidents")[0], 200)
        os.remove(self.hold)
        self.assertEqual(self.req("/board/test", "POST", {"name": "crc32"})[0], 202)

    def test_other_routes_404(self):
        for path, method in (("/patch", "POST"), ("/bisect", "POST"), ("/shell", "POST"),
                             ("/nope", "GET"), ("/incidents/1/x", "GET"), ("/run", "GET"),
                             ("/run", "PUT"), ("/incidents/1", "DELETE")):
            self.assertEqual(self.req(path, method, {} if method != "GET" else None)[0], 404,
                             (method, path))

    def test_clean_caps_and_strips(self):
        self.assertEqual(serve.clean("a\x1b[31mbé"), "a?[31mb?")
        self.assertEqual(len(serve.clean("x" * 500)), 120)
        self.assertEqual(serve.clean({"k": ["\n"]}), {"k": ["?"]})


class StubServeCliTest(unittest.TestCase):
    def test_refuses_empty_token_file(self):
        with tempfile.TemporaryDirectory() as d:
            tok = os.path.join(d, "bench_token")
            open(tok, "w").close()
            db = os.path.join(d, "bench.db")
            make_db.build(db)
            self.assertEqual(serve.main(["--db", db, "--token-file", tok, "--port", "0"]), 1)


if __name__ == "__main__":
    unittest.main()
