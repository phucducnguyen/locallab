"""Lane B: the GB10's live agent command config (agent/agent.gb10.json)."""

import json
import os
import unittest

from agent import explain

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GB10 = os.path.join(ROOT, "agent", "agent.gb10.json")


class Gb10Config(unittest.TestCase):
    def setUp(self):
        self.cfg = explain.load_config(GB10)

    def test_loads_without_placeholders(self):
        self.assertTrue(self.cfg["agent_cmd"])

    def test_one_turn_in_the_labwatch_sandbox(self):
        cmd = self.cfg["agent_cmd"]
        i = cmd.index("labwatch")
        self.assertTrue(cmd[i - 1].endswith("/nemoclaw"))
        self.assertEqual(cmd[i + 1], "agent")
        self.assertIn("NEMOCLAW_GATEWAY_PORT=8090", cmd)
        self.assertNotIn("--deliver", cmd)  # the agent never posts
        self.assertNotIn("--channel", cmd)

    def test_fresh_session_per_call(self):
        a, msg, s1 = explain.build_command(self.cfg, "incident", "3")
        b, _, s2 = explain.build_command(self.cfg, "incident", "3")
        self.assertNotEqual(s1, s2)
        self.assertEqual(a[a.index("--session-id") + 1], s1)
        self.assertEqual(b[b.index("--session-id") + 1], s2)
        self.assertEqual(a[a.index("-m") + 1], msg)
        self.assertIn("incident 3", msg)

    def test_timeouts_fit_the_bench_minute(self):
        cmd = self.cfg["agent_cmd"]
        inner = float(cmd[cmd.index("--timeout") + 1])
        self.assertLess(inner, self.cfg["timeout_s"])
        self.assertLess(self.cfg["timeout_s"], 60)

    def test_no_secret_paths(self):
        with open(GB10, encoding="utf-8") as f:
            text = f.read()
        for bad in ("token", ".config/labwatch", "telegram", "tracker"):
            self.assertNotIn(bad, text.lower())
        json.loads(text)


if __name__ == "__main__":
    unittest.main()
