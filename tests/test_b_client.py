"""Lane B: the skill's client against the stub endpoint, on every route."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from stub import make_db, serve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "agent", "skill", "labwatch", "labwatch.py")
TOKEN = "fake-bench-token-b"


def load_client():
    spec = importlib.util.spec_from_file_location("lw_skill_client", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ in the skill
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    return mod


labwatch = load_client()


class StubFixture(unittest.TestCase):
    """A stub endpoint on 127.0.0.1, a fake token file and a client config."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "bench.db")
        make_db.build(cls.db)
        cls.hold = os.path.join(cls.tmp.name, "hold")
        cls.token_file = os.path.join(cls.tmp.name, "bench_token")
        with open(cls.token_file, "w") as f:
            f.write(TOKEN + "\n")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.audit = io.StringIO()
        self.srv = serve.make_server(self.db, "127.0.0.1", 0, TOKEN, self.hold, self.audit)
        self.requests = []
        handler = self.srv.RequestHandlerClass
        seen = self.requests

        class Counting(handler):
            def do_GET(self):
                seen.append(("GET", self.path))
                return handler.do_GET(self)

            def do_POST(self):
                seen.append(("POST", self.path))
                return handler.do_POST(self)

        self.srv.RequestHandlerClass = Counting
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.config = self.write_config()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        if os.path.exists(self.hold):
            os.remove(self.hold)

    def write_config(self, token_file=None, name="client.json"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            json.dump({"base_url": "http://127.0.0.1:%d" % self.srv.server_address[1],
                       "token_file": token_file or self.token_file, "proxy": "none",
                       "timeout_s": 5}, f)
        return path

    def client(self, *argv, config=None):
        out, err = io.StringIO(), io.StringIO()
        rc = labwatch.main(["--config", config or self.config] + list(argv), out, err)
        body = json.loads(out.getvalue()) if out.getvalue() else None
        return rc, body, err.getvalue()


class ClientRoutesTest(StubFixture):
    def test_health(self):
        rc, r, _ = self.client("health")
        self.assertEqual((rc, r["status"]), (0, 200))
        self.assertIs(r["body"]["ok"], True)

    def test_board(self):
        rc, r, _ = self.client("board")
        self.assertEqual(rc, 0)
        self.assertEqual(r["body"]["boards"][0]["board"], "nucleo-1")

    def test_incidents_all_and_open(self):
        rc, r, _ = self.client("incidents")
        self.assertEqual(rc, 0)
        self.assertEqual([i["id"] for i in r["body"]["incidents"]], [3, 2, 1])
        rc, r, _ = self.client("incidents", "--open")
        self.assertEqual(rc, 0)
        self.assertNotIn(1, [i["id"] for i in r["body"]["incidents"]])
        self.assertIn(("GET", "/incidents?open=1"), self.requests)

    def test_incident(self):
        rc, r, _ = self.client("incident", "3")
        self.assertEqual(rc, 0)
        self.assertEqual((r["body"]["triage"], r["body"]["routing"][0]["route"]),
                         ("CODE", "Firmware team"))

    def test_incident_missing_is_exit_1(self):
        rc, r, _ = self.client("incident", "99")
        self.assertEqual((rc, r["status"]), (1, 404))

    def test_runs_and_run(self):
        rc, r, _ = self.client("runs")
        self.assertEqual(rc, 0)
        self.assertEqual([x["run_id"] for x in r["body"]["runs"]], [2, 1])
        rc, r, _ = self.client("run", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(r["body"]["verdict"], "HARDFAULT")
        self.assertIn("subject", r["body"]["data"])

    def test_events_since(self):
        rc, r, _ = self.client("events", "--since", "10")
        self.assertEqual(rc, 0)
        self.assertTrue(all(e["id"] > 10 for e in r["body"]["events"]))
        self.assertIn(("GET", "/events?since=10"), self.requests)

    def test_show_a_sha(self):
        rc, r, _ = self.client("show", "2222222")
        self.assertEqual(rc, 0)
        self.assertTrue(any(ln.startswith("+") for ln in r["body"]["data"]["diff"]))

    def test_post_run_then_429(self):
        rc, r, _ = self.client("post-run", "2222222")
        self.assertEqual((rc, r["status"]), (0, 202))
        rc, r, _ = self.client("post-run", "2222222")
        self.assertEqual((rc, r["status"]), (1, 429))

    def test_board_test_and_hold(self):
        rc, r, _ = self.client("board-test", "loopback")
        self.assertEqual((rc, r["status"]), (0, 202))
        with open(self.hold, "w") as f:
            f.write("test 0\n")
        rc, r, _ = self.client("board-test", "loopback")
        self.assertEqual((rc, r["status"]), (1, 429))

    def test_wrong_token_is_401_and_never_printed(self):
        bad = os.path.join(self.tmp.name, "wrong_token")
        with open(bad, "w") as f:
            f.write("not-the-token\n")
        rc, r, err = self.client("incident", "1", config=self.write_config(bad, "bad.json"))
        self.assertEqual((rc, r["status"]), (1, 401))
        self.assertNotIn("not-the-token", json.dumps(r) + err)

    def test_token_value_never_in_output(self):
        for argv in (["health"], ["incident", "3"], ["show", "2222222"], ["show", "HEAD"]):
            out, err = io.StringIO(), io.StringIO()
            labwatch.main(["--config", self.config] + argv, out, err)
            self.assertNotIn(TOKEN, out.getvalue() + err.getvalue())

    def test_as_a_script(self):
        p = subprocess.run([sys.executable, "-I", CLIENT, "--config", self.config, "incident", "1"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["body"]["triage"], "HARDWARE")

    def test_config_from_env(self):
        env = dict(os.environ, LW_CLIENT_CONFIG=self.config)
        p = subprocess.run([sys.executable, "-I", CLIENT, "health"], capture_output=True,
                           text=True, timeout=30, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)


class ClientRefusalsTest(StubFixture):
    """Anything but a sha to /show (and bad ids, names, configs) never leaves the client."""

    BAD_REFS = ["main:README.md", "--output=x", "HEAD", "main", "feature/x", "2222222\n",
                "222222", "2222222G", "ABCDEF1", "2222222 --stat", "2222222;ls",
                "a" * 41, "", "../2222222", "2222222?ref=1"]

    def test_show_refuses_non_shas_before_sending(self):
        for ref in self.BAD_REFS:
            with self.subTest(ref=ref):
                rc, out, err = self.client("show", ref)
                self.assertEqual(rc, 2)
                self.assertIsNone(out)
                self.assertTrue(err)  # "refused: ..." or a usage error for option-like refs
        self.assertEqual(self.requests, [])

    def test_post_run_refuses_non_shas(self):
        for ref in ("HEAD", "--x", "2222222\n"):
            rc, _, _ = self.client("post-run", ref)
            self.assertEqual(rc, 2)
        self.assertEqual(self.requests, [])

    def test_board_test_refuses_unknown_names(self):
        for name in ("reset", "loopback;ls", "", "LOOPBACK"):
            rc, _, _ = self.client("board-test", name)
            self.assertEqual(rc, 2)
        self.assertEqual(self.requests, [])

    def test_ids_must_be_numbers(self):
        for argv in (["incident", "1/../../show"], ["run", "-1"], ["events", "--since", "x"]):
            rc, _, _ = self.client(*argv)
            self.assertEqual(rc, 2)
        self.assertEqual(self.requests, [])

    def test_stub_also_refuses(self):
        """The server side agrees: a non-sha sent by hand is a 400."""
        cfg = labwatch.load_config(self.config)
        for ref in ("main:README.md", "--output=x", "HEAD"):
            status, _ = labwatch.request(cfg, "GET", "/show?ref=" + labwatch.quote(ref, safe=""))
            self.assertEqual(status, 400)

    def test_bad_config_is_refused(self):
        path = os.path.join(self.tmp.name, "broken.json")
        for cfg in ({"base_url": "file:///etc/passwd", "token_file": self.token_file},
                    {"base_url": "http://127.0.0.1:1"}, ["not", "an", "object"]):
            with open(path, "w") as f:
                json.dump(cfg, f)
            rc, _, _ = self.client("health", config=path)
            self.assertEqual(rc, 2)
        rc, _, _ = self.client("health", config=os.path.join(self.tmp.name, "missing.json"))
        self.assertEqual(rc, 2)

    def test_redirects_are_not_followed(self):
        import http.server

        hits = []

        class Redirect(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/health" % self.server.server_address[1])
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            path = os.path.join(self.tmp.name, "redir.json")
            with open(path, "w") as f:
                json.dump({"base_url": "http://127.0.0.1:%d" % srv.server_address[1],
                           "token_file": self.token_file, "proxy": "none"}, f)
            rc, r, _ = self.client("incident", "1", config=path)
            self.assertEqual((rc, r["status"]), (1, 302))
            self.assertEqual(len(hits), 1)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
