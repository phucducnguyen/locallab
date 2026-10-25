"""Lane B: agent.explain with a fake agent command (a script printing canned replies)."""

import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest

from agent import explain
from tests.test_b_client import CLIENT, StubFixture

GOOD = ("The board faulted in its own run [serial 09:40:02.150].\n"
        "DIAGNOSIS: CODE | VERDICT: HARDFAULT | COMMIT: 2222222 | CAUSE: pointer\n")

FAKE_AGENT = textwrap.dedent('''
    import json, os, sys, time
    mode, log = sys.argv[1], sys.argv[2]
    rest = sys.argv[3:]
    msg = rest[1] if len(rest) > 1 else sys.stdin.read()
    with open(log, "a") as f:
        f.write(json.dumps({"session": rest[0] if rest else None, "message": msg}) + "\\n")
    good = %r
    if mode == "good":
        sys.stdout.write(good)
    elif mode == "good-crlf":
        sys.stdout.write(good.replace("\\n", "\\r\\n") + "\\n\\n")
    elif mode == "no-diag":
        print("The board faulted [serial 09:40:02.150].")
    elif mode == "trailing":
        sys.stdout.write(good + "Anything else?\\n")
    elif mode == "bad-format":
        print("DIAGNOSIS: MAYBE | VERDICT: HARDFAULT | COMMIT: 2222222 | CAUSE: pointer")
    elif mode == "two-words":
        print("DIAGNOSIS: CODE | VERDICT: HARDFAULT | COMMIT: 2222222 | CAUSE: bad pointer")
    elif mode == "fail":
        sys.stdout.write(good)
        sys.exit(3)
    elif mode == "sleep":
        sys.stdout.write("thinking\\n")
        sys.stdout.flush()
        time.sleep(30)
        sys.stdout.write(good)
    elif mode == "client":
        # Plays the agent: reads the record with the skill client, cites what it got.
        import subprocess
        cfg, rid = os.environ["FAKE_CLIENT_CONFIG"], msg.split("incident ")[1].split(":")[0]
        p = subprocess.run([sys.executable, "-I", os.environ["FAKE_CLIENT"], "--config", cfg,
                            "incident", rid], capture_output=True, text=True)
        rec = json.loads(p.stdout)["body"]
        ts = rec["evidence"]["lines"][0]["raw_ts"]
        print("The board reported %%s [serial %%s]." %% (rec["kind"], ts))
        print("The bench triaged it %%s [incident %%d]." %% (rec["triage"], rec["id"]))
        print("DIAGNOSIS: %%s | VERDICT: %%s | COMMIT: none | CAUSE: loopback"
              %% (rec["triage"], rec["kind"]))
''') % GOOD


class ExplainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = os.path.join(self.tmp.name, "fake_agent.py")
        with open(self.fake, "w") as f:
            f.write(FAKE_AGENT)
        self.log = os.path.join(self.tmp.name, "calls.jsonl")

    def config(self, mode, via="arg", timeout=10, extra=None):
        cmd = [sys.executable, "-I", self.fake, mode, self.log, "{session}"]
        if via == "arg":
            cmd.append("{message}")
        cfg = {"agent_cmd": cmd, "timeout_s": timeout}
        cfg.update(extra or {})
        path = os.path.join(self.tmp.name, "agent-%s.json" % mode)
        with open(path, "w") as f:
            json.dump(cfg, f)
        return path

    def run_explain(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = explain.main(list(argv), out, err)
        return rc, out.getvalue(), err.getvalue()

    def calls(self):
        with open(self.log) as f:
            return [json.loads(x) for x in f]

    def test_good_reply_exit_0_and_printed(self):
        rc, out, _ = self.run_explain("--incident", "3", "--config", self.config("good"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, GOOD)
        self.assertIn("incident 3", self.calls()[0]["message"])

    def test_run_record(self):
        rc, _, _ = self.run_explain("--run", "2", "--config", self.config("good"))
        self.assertEqual(rc, 0)
        self.assertIn("run 2", self.calls()[0]["message"])

    def test_crlf_and_blank_tail_are_tolerated(self):
        rc, out, _ = self.run_explain("--incident", "3", "--config", self.config("good-crlf"))
        self.assertEqual(rc, 0)
        self.assertNotIn("\r", out)

    def test_message_on_stdin_when_no_placeholder(self):
        rc, _, _ = self.run_explain("--incident", "3", "--config", self.config("good", via="stdin"))
        self.assertEqual(rc, 0)
        self.assertIn("incident 3", self.calls()[0]["message"])

    def test_fresh_session_every_call(self):
        cfg = self.config("good")
        for _ in range(3):
            self.assertEqual(self.run_explain("--incident", "3", "--config", cfg)[0], 0)
        sessions = [c["session"] for c in self.calls()]
        self.assertEqual(len(set(sessions)), 3)
        self.assertTrue(all(s.startswith("lw-incident-3-") for s in sessions))

    def test_bad_final_lines_exit_2(self):
        for mode in ("no-diag", "trailing", "bad-format", "two-words"):
            with self.subTest(mode=mode):
                rc, out, err = self.run_explain("--incident", "3", "--config", self.config(mode))
                self.assertEqual(rc, 2)
                self.assertTrue(out)  # the reply is still printed
                self.assertIn("DIAGNOSIS", err)

    def test_agent_failure_exit_2(self):
        rc, _, err = self.run_explain("--incident", "3", "--config", self.config("fail"))
        self.assertEqual(rc, 2)
        self.assertIn("exited 3", err)

    def test_timeout_exit_2_and_agent_killed(self):
        t0 = time.monotonic()
        rc, out, err = self.run_explain("--incident", "3", "--config",
                                        self.config("sleep", timeout=30), "--timeout", "1")
        self.assertEqual(rc, 2)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn("no answer within", err)
        self.assertNotIn("DIAGNOSIS", out)

    def test_timeout_from_config(self):
        rc, _, _ = self.run_explain("--incident", "3", "--config", self.config("sleep", timeout=1))
        self.assertEqual(rc, 2)

    def test_default_timeout_is_55(self):
        self.assertEqual(explain.DEFAULT_TIMEOUT_S, 55.0)
        path = os.path.join(self.tmp.name, "min.json")
        with open(path, "w") as f:
            json.dump({"agent_cmd": ["true"]}, f)
        self.assertEqual(explain.load_config(path)["timeout_s"], 55.0)

    def test_config_errors_exit_2(self):
        example = os.path.join(os.path.dirname(explain.__file__), "agent.example.json")
        missing = os.path.join(self.tmp.name, "missing.json")
        notalist = os.path.join(self.tmp.name, "bad.json")
        with open(notalist, "w") as f:
            json.dump({"agent_cmd": "openclaw run"}, f)
        for path in (example, missing, notalist):
            with self.subTest(path=path):
                rc, out, _ = self.run_explain("--incident", "3", "--config", path)
                self.assertEqual((rc, out), (2, ""))

    def test_config_from_env(self):
        os.environ["LW_AGENT_CONFIG"] = self.config("good")
        self.addCleanup(os.environ.pop, "LW_AGENT_CONFIG")
        self.assertEqual(self.run_explain("--incident", "3")[0], 0)

    def test_missing_agent_binary_exit_2(self):
        path = os.path.join(self.tmp.name, "nobin.json")
        with open(path, "w") as f:
            json.dump({"agent_cmd": [os.path.join(self.tmp.name, "no-such-cli"), "{message}"]}, f)
        self.assertEqual(self.run_explain("--incident", "3", "--config", path)[0], 2)

    def test_bad_ids(self):
        for rid in ("3; rm", "-1", "x", ""):
            self.assertEqual(self.run_explain("--incident", rid, "--config",
                                              self.config("good"))[0], 2)
        self.assertFalse(os.path.exists(self.log))

    def test_parse_diagnosis(self):
        d = explain.parse_diagnosis(GOOD.splitlines()[-1])
        self.assertEqual(d, {"diagnosis": "CODE", "verdict": "HARDFAULT", "commit": "2222222",
                             "cause": "pointer"})
        for line in ("DIAGNOSIS: HARDWARE | VERDICT: HEALTH_FAIL | COMMIT: none | CAUSE: jumper",
                     "DIAGNOSIS: UNKNOWN | VERDICT: NO_OUTPUT | COMMIT: none | CAUSE: unconfirmed",
                     "DIAGNOSIS: NONE | VERDICT: PASS | COMMIT: none | CAUSE: none"):
            self.assertIsNotNone(explain.parse_diagnosis(line), line)
        for line in ("DIAGNOSIS: CODE | VERDICT: HARDFAULT | COMMIT: HEAD | CAUSE: x",
                     "DIAGNOSIS: CODE | VERDICT: CRASH | COMMIT: none | CAUSE: x",
                     "DIAGNOSIS: CODE | VERDICT: HARDFAULT | COMMIT: none | CAUSE: ",
                     "Diagnosis: CODE | VERDICT: HARDFAULT | COMMIT: none | CAUSE: x",
                     "DIAGNOSIS: CODE | VERDICT: HARDFAULT | COMMIT: none | CAUSE: x | EXTRA: y"):
            self.assertIsNone(explain.parse_diagnosis(line), line)


class ExplainAgainstStubTest(StubFixture):
    """A fake agent that reads the stub through the skill client, end to end."""

    def test_fake_agent_uses_the_client(self):
        tmp = self.tmp.name
        fake = os.path.join(tmp, "fake_agent.py")
        with open(fake, "w") as f:
            f.write(FAKE_AGENT)
        cfg = os.path.join(tmp, "agent-client.json")
        with open(cfg, "w") as f:
            json.dump({"agent_cmd": [sys.executable, "-I", fake, "client",
                                     os.path.join(tmp, "calls.jsonl"), "{session}", "{message}"]}, f)
        os.environ.update(FAKE_CLIENT=CLIENT, FAKE_CLIENT_CONFIG=self.config)
        self.addCleanup(os.environ.pop, "FAKE_CLIENT")
        self.addCleanup(os.environ.pop, "FAKE_CLIENT_CONFIG")
        out, err = io.StringIO(), io.StringIO()
        rc = explain.main(["--incident", "1", "--config", cfg], out, err)
        self.assertEqual(rc, 0, err.getvalue())
        self.assertTrue(out.getvalue().rstrip().endswith(
            "DIAGNOSIS: HARDWARE | VERDICT: HEALTH_FAIL | COMMIT: none | CAUSE: loopback"))
        self.assertIn(("GET", "/incidents/1"), self.requests)
        self.assertNotIn("POST", [m for m, _ in self.requests])


class ExplainModuleTest(unittest.TestCase):
    setUp = ExplainTest.setUp
    config = ExplainTest.config

    def test_as_a_module(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.run([sys.executable, "-m", "agent.explain", "--incident", "3", "--config",
                            self.config("good")], cwd=root, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, GOOD)


if __name__ == "__main__":
    unittest.main()
