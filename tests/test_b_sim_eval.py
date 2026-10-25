"""Lane B: agent.sim_eval against a fake OpenAI-compatible server on 127.0.0.1 (no real model)."""

import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent import sim_eval
from tests.test_b_client import ROOT

RECORD_RE = re.compile(r"\$ python3 labwatch\.py (?:incident|run) \d+\n(\{.*?\n\})", re.S)
SHOW_RE = re.compile(r"\$ python3 labwatch\.py show [0-9a-f]+\n(\{.*?\n\})", re.S)


def good_reply(record, show):
    """A rule-following reply built from what the model was shown."""
    lines = record["evidence"].get("lines") or []
    cite = ("[incident %d]" % record["id"]) if "kind" in record else (
        "[serial %s]" % lines[0]["raw_ts"] if lines else "")
    out = []
    if lines:
        out.append("The board printed %s [serial %s]." % (lines[0]["line"], lines[0]["raw_ts"]))
    out.append("The bench's triage is %s %s." % (record["triage"], cite or "(likely)"))
    sha = "none"
    if record["triage"] == "CODE":
        sha = record.get("firmware") or record.get("commit")
        if show:
            out.append("The commit changed one line [diff %s +11]." % sha)
    out.append("Next step: likely a second look at the record.")
    verdict = record.get("kind") or record.get("verdict")
    out.append("DIAGNOSIS: %s | VERDICT: %s | COMMIT: %s | CAUSE: x" % (record["triage"], verdict,
                                                                      sha))
    return "\n".join(out)


def make_reply(mode, user):
    record = json.loads(RECORD_RE.search(user).group(1))["body"]
    m = SHOW_RE.search(user)
    show = json.loads(m.group(1))["body"] if m else None
    good = good_reply(record, show)
    if mode == "good":
        return good
    if mode == "wrong-label":
        return re.sub(r"DIAGNOSIS: \w+", "DIAGNOSIS: NONE" if record["triage"] != "NONE"
                      else "DIAGNOSIS: CODE", good)
    if mode == "trailing":
        return good + "\nHope this helps!"
    if mode == "uncited":
        return "The board did something odd.\n" + good
    if mode == "unresolved":
        return "The board faulted [serial 23:59:59.999] [src/nowhere.c:1].\n" + good
    if mode == "cant-tell":
        return "I can't tell from this record [incident 1].\n" + good
    if mode == "invented":
        return "The board said loopback=CLOSED at 0xdeadbee0 [incident 1].\n" + good
    if mode == "hw-commit":
        sha = record.get("firmware") or record.get("commit") or "abcdef1"
        return re.sub(r"COMMIT: none", "COMMIT: %s" % sha, good)
    if mode == "no-diag":
        return "The board did something [incident 1]."
    raise AssertionError(mode)


class FakeOpenAI:
    def __init__(self, mode="good"):
        self.mode, self.payloads, self.lock = mode, [], threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake.lock:
                    fake.payloads.append((self.path, body))
                if fake.mode == "http-500":
                    self.send_response(500)
                    self.end_headers()
                    return
                user = body["messages"][-1]["content"]
                if fake.mode == "truncated":
                    text, finish = make_reply("good", user)[:40], "length"
                else:
                    text, finish = make_reply(fake.mode, user), "stop"
                data = json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                                                "finish_reason": finish}],
                                   "usage": {"prompt_tokens": 900, "completion_tokens": 120}})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data.encode())

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class GeneratorTest(unittest.TestCase):
    def test_every_variant_builds_and_a_rule_following_reply_scores_clean(self):
        for name in sorted(sim_eval.VARIANTS):
            for i in range(6):
                with self.subTest(variant=name, i=i):
                    c = sim_eval.make_case("t", i, [name])
                    self.assertEqual(c["record"]["triage"], c["expect"]["triage"])
                    if c["target"] == "incident":
                        self.assertEqual(c["record"]["kind"], c["expect"]["verdict"])
                    else:
                        self.assertEqual(c["record"]["verdict"], c["expect"]["verdict"])
                    if c["expect"]["triage"] == "CODE":
                        self.assertIsNotNone(c["show"])
                        self.assertEqual(c["expect"]["commit"],
                                         c["record"].get("firmware") or c["record"].get("commit"))
                    s = sim_eval.score(c, sim_eval.oracle_reply(c))
                    self.assertEqual(s["invented"], [])
                    for k in ("format_ok", "label_ok", "verdict_ok", "commit_ok", "cite_clean"):
                        self.assertTrue(s[k], (k, s))

    def test_same_seed_same_case_and_values_vary(self):
        a, b = sim_eval.make_case(7, 3), sim_eval.make_case(7, 3)
        self.assertEqual(a, b)
        shas = {sim_eval.make_case(7, i)["record"].get("firmware") for i in range(0, 60, 14)}
        self.assertGreater(len(shas), 3)

    def test_file_line_points_at_an_added_diff_line(self):
        for i in range(30):
            c = sim_eval.make_case("d", i, ["hardfault"])
            fname, line = c["show"]["file_line"].rsplit(":", 1)
            diff = c["show"]["data"]["diff"]
            hunk = next(k for k, ln in enumerate(diff) if ln.startswith("@@"))
            new = int(re.search(r"\+(\d+),", diff[hunk]).group(1))
            for ln in diff[hunk + 1:]:
                if ln.startswith("+") and new == int(line):
                    break
                if not ln.startswith("-"):
                    new += 1
            else:
                self.fail("file_line %s is not an added line" % c["show"]["file_line"])
            self.assertTrue(fname.startswith("src/demo"))

    def test_values_follow_the_invented_shapes(self):
        cases = [sim_eval.make_case("v", i) for i in range(140)]
        blob = json.dumps(cases)
        for fl in re.findall(r'"file_line": "([^"]+)"', blob):
            self.assertRegex(fl, r"^src/demo\w*\.c:\d+$")
        for c in cases:
            regs = c["record"]["evidence"].get("registers")
            if regs:
                self.assertGreaterEqual(bin(int(regs["cfsr"], 16)).count("1"), 2)
        for m in re.finditer(r"LW TEST \w+ FAIL ([^\"]+)", blob):
            self.assertRegex(m.group(1), r"^(step=\d+ code=\d+|open|stuck)$")

    def test_messages_hold_the_skill_and_the_client_output(self):
        c = sim_eval.make_case("m", 0, ["hardfault"])
        msgs = sim_eval.build_messages(c, "RULES")
        self.assertEqual(msgs[0], {"role": "system", "content": "RULES"})
        self.assertIn("$ python3 labwatch.py %s %d" % (c["target"], c["id"]), msgs[1]["content"])
        self.assertIn("$ python3 labwatch.py show %s" % c["show"]["ref"], msgs[1]["content"])
        self.assertEqual(json.loads(RECORD_RE.search(msgs[1]["content"]).group(1))["body"],
                         c["record"])


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.skill = os.path.join(self.tmp.name, "SKILL.md")
        with open(self.skill, "w") as f:
            f.write("THE RULES\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_mode(self, mode, trials=28, extra=()):
        fake = FakeOpenAI(mode)
        out_dir = os.path.join(self.tmp.name, mode)
        try:
            with redirect_stderr(io.StringIO()):
                rc = sim_eval.main(["--trials", str(trials), "--concurrency", "4", "--out",
                                    out_dir, "--base-url", fake.url, "--skill", self.skill,
                                    "--no-gpu", "--seed", "e2e", "--pause-file",
                                    os.path.join(self.tmp.name, "PAUSE")] + list(extra), out=io.StringIO())
        finally:
            fake.close()
        self.assertEqual(rc, 0)
        with open(os.path.join(out_dir, "summary.json")) as f:
            summary = json.load(f)
        with open(os.path.join(out_dir, "trials.jsonl")) as f:
            rows = [json.loads(ln) for ln in f]
        self.assertEqual(len(rows), trials)
        self.assertTrue(os.path.exists(os.path.join(out_dir, "summary.txt")))
        return summary, rows, fake

    def test_good_replies_score_clean(self):
        s, rows, fake = self.run_mode("good")
        o = s["overall"]
        for k in ("label_acc", "format_rate", "verdict_acc", "commit_acc", "cite_clean"):
            self.assertEqual(o[k], 100.0, k)
        self.assertEqual(o["errors"], 0)
        self.assertEqual(o["cant_tell"], 0.0)
        self.assertEqual(rows[0]["tok_s"] > 0, True)
        path, body = fake.payloads[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(body["model"], sim_eval.DEFAULT_MODEL)
        self.assertEqual(body["messages"][0]["content"], "THE RULES\n")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(len(s["variants"]), len(sim_eval.VARIANTS))

    def test_wrong_label(self):
        s, rows, _ = self.run_mode("wrong-label")
        self.assertEqual(s["overall"]["label_acc"], 0.0)
        self.assertEqual(s["overall"]["format_rate"], 100.0)
        self.assertTrue(any(k.startswith("label_") for k in s["failures"]))

    def test_trailing_text_is_a_format_failure(self):
        s, _, _ = self.run_mode("trailing")
        self.assertEqual(s["overall"]["format_rate"], 0.0)
        self.assertEqual(s["overall"]["label_acc"], 100.0)
        self.assertIn("diagnosis_not_last", s["failures"])

    def test_no_diagnosis(self):
        s, _, _ = self.run_mode("no-diag", trials=8)
        self.assertEqual(s["overall"]["label_acc"], 0.0)
        self.assertEqual(s["failures"]["no_diagnosis"], 8)

    def test_uncited_and_unresolved(self):
        s, rows, _ = self.run_mode("uncited", trials=8)
        self.assertEqual(s["overall"]["cite_clean"], 0.0)
        self.assertEqual(s["failures"]["uncited_sentence"], 8)
        s, rows, _ = self.run_mode("unresolved", trials=8)
        self.assertEqual(s["overall"]["cite_clean"], 0.0)
        self.assertEqual(s["failures"]["unresolved_citation"], 8)
        self.assertGreaterEqual(rows[0]["unresolved"], 2)

    def test_invented_values_and_commit_for_non_code(self):
        s, rows, _ = self.run_mode("invented", trials=8)
        self.assertEqual(s["overall"]["values_exact"], 0.0)
        self.assertIn("loopback=CLOSED", rows[0]["invented"])
        self.assertIn("0xdeadbee0", rows[0]["invented"])
        s, rows, _ = self.run_mode("good", trials=14)
        self.assertEqual(s["overall"]["values_exact"], 100.0)
        s, rows, _ = self.run_mode("hw-commit", trials=14)
        hw = [r for r in rows if r["expect"]["triage"] == "HARDWARE"
              and r["variant"] != "run_pass"]
        self.assertTrue(hw)
        for r in hw:
            self.assertFalse(r["commit_ok"])
            self.assertIn("commit_named_for_HARDWARE", r["failures"])

    def test_cant_tell_counted(self):
        s, _, _ = self.run_mode("cant-tell", trials=8)
        self.assertEqual(s["overall"]["cant_tell"], 100.0)

    def test_http_error_and_truncation(self):
        s, rows, _ = self.run_mode("http-500", trials=4)
        self.assertEqual(s["overall"]["errors"], 4)
        self.assertEqual(s["failures"]["http_error"], 4)
        self.assertTrue(all("error" in r for r in rows))
        s, rows, _ = self.run_mode("truncated", trials=4)
        self.assertEqual(s["failures"]["truncated"], 4)

    def test_think_on_sends_no_template_kwargs(self):
        _, _, fake = self.run_mode("good", trials=2, extra=("--think", "on"))
        self.assertNotIn("chat_template_kwargs", fake.payloads[0][1])

    def test_pause_file_holds_requests(self):
        pause = os.path.join(self.tmp.name, "PAUSE")
        open(pause, "w").close()
        threading.Timer(1.0, os.remove, (pause,)).start()
        t = time.monotonic()
        with redirect_stderr(io.StringIO()):
            sim_eval.wait_while_paused(pause, poll=0.1)
        self.assertGreaterEqual(time.monotonic() - t, 0.9)
        self.assertFalse(os.path.exists(pause))

    def test_out_inside_the_repo_is_refused(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            sim_eval.main(["--out", os.path.join(ROOT, "sim-out"), "--dry-run"])
        self.assertFalse(os.path.exists(os.path.join(ROOT, "sim-out")))


if __name__ == "__main__":
    unittest.main()
