"""Lane B: the sandbox network policy preset (agent/policy/)."""

import argparse
import importlib.util
import os
import re
import sys
import unittest

from agent.policy import build

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "agent", "skill", "labwatch", "labwatch.py")
POLICY_DIR = os.path.join(ROOT, "agent", "policy")


def load_client():
    spec = importlib.util.spec_from_file_location("lw_skill_client_p", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    return mod


def endpoints(pol):
    for key, entry in pol["network_policies"].items():
        for ep in entry["endpoints"]:
            yield key, entry, ep


def path_allowed(pol, method, path):
    """Mirror of a REST rule match: a '*' segment never crosses a '/'."""
    path = path.split("?", 1)[0]
    for _, _, ep in endpoints(pol):
        for r in ep["rules"]:
            a = r["allow"]
            pat = re.escape(a["path"]).replace(r"\*", "[^/]+")
            if a["method"] == method and re.fullmatch(pat, path):
                return True
    return False


class PolicyFile(unittest.TestCase):
    def test_committed_yaml_is_generated(self):
        with open(build.YAML_PATH, encoding="utf-8") as f:
            self.assertEqual(f.read(), build.render())

    def test_preset_name_is_a_label(self):
        self.assertRegex(build.policy()["preset"]["name"], r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

    def test_keys_match_names(self):
        for key, entry in build.policy()["network_policies"].items():
            self.assertEqual(key, entry["name"])

    def test_only_the_bench_on_the_bridge(self):
        pol = build.policy()
        eps = list(endpoints(pol))
        self.assertEqual(len(eps), 1)
        _, _, ep = eps[0]
        self.assertEqual(ep["host"], "host.openshell.internal")
        self.assertEqual(ep["port"], 8765)
        self.assertEqual(ep["allowed_ips"], ["172.18.0.1/32"])
        self.assertEqual((ep["protocol"], ep["enforcement"]), ("rest", "enforce"))

    def test_bridge_ip_is_a_parameter(self):
        _, _, ep = next(endpoints(build.policy("10.9.8.7")))
        self.assertEqual(ep["allowed_ips"], ["10.9.8.7/32"])

    def test_no_outside_host_anywhere(self):
        text = build.render().lower()
        for bad in ("github", "telegram", "example.com", "forgejo", "jira", "0.0.0.0", "/0",
                    "access: ", "tls: skip", "/**"):
            self.assertNotIn(bad, text)

    def test_rules_are_the_interface_routes_only(self):
        _, _, ep = next(endpoints(build.policy()))
        got = {(r["allow"]["method"], r["allow"]["path"]) for r in ep["rules"]}
        self.assertEqual(got, set(build.BENCH_RULES))
        posts = {p for m, p in got if m == "POST"}
        self.assertEqual(posts, {"/run", "/board/test"})
        self.assertEqual({m for m, _ in got}, {"GET", "POST"})

    def test_only_python_reaches_the_bench(self):
        entry = build.policy()["network_policies"]["labwatch_bench"]
        for b in entry["binaries"]:
            self.assertTrue(b["path"].startswith("/usr/bin/python3"), b)
            self.assertNotIn("*", b["path"])

    def test_every_client_route_is_allowed(self):
        client = load_client()
        pol = build.policy()
        cases = [("health", {}), ("board", {}), ("incidents", {"open": True}),
                 ("incidents", {"open": False}), ("incident", {"id": "3"}), ("runs", {}),
                 ("run", {"id": "2"}), ("events", {"since": "0"}), ("show", {"sha": "2222222"}),
                 ("post-run", {"sha": "2222222"}), ("board-test", {"name": "loopback"})]
        for cmd, kw in cases:
            method, path, _ = client.build(cmd, argparse.Namespace(**kw))
            self.assertTrue(path_allowed(pol, method, path), (cmd, method, path))

    def test_off_interface_routes_are_not_allowed(self):
        pol = build.policy()
        for method, path in [("POST", "/patch"), ("POST", "/bisect"), ("POST", "/shell"),
                             ("GET", "/incidents/3/x"), ("DELETE", "/runs/1"),
                             ("PUT", "/run"), ("POST", "/show"), ("GET", "/")]:
            self.assertFalse(path_allowed(pol, method, path), (method, path))

    def test_check_mode(self):
        self.assertEqual(build.main(["--check"]), 0)


class Scripts(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(POLICY_DIR, name), encoding="utf-8") as f:
            return f.read()

    def test_apply_removes_every_internet_entry(self):
        text = self.read("apply.sh")
        for p in ("brew-balanced", "huggingface", "npm", "openclaw-pricing", "pypi",
                  "clawhub", "npm_registry", "nvidia", "openclaw_api", "openclaw_docs"):
            self.assertIn(p, text)
        self.assertIn("policy add --from-file", text)
        self.assertNotIn("telegram", text.lower())

    def test_scripts_never_touch_tokens(self):
        for name in ("apply.sh", "prove.sh"):
            text = self.read(name)
            self.assertNotIn("bench_token", text)
            self.assertNotIn(".config/labwatch", text)

    def test_prove_covers_the_sunday_row(self):
        text = self.read("prove.sh")
        for host in ("github.com", "example.com"):
            self.assertIn('connect("%s")' % host, text)
        self.assertIn("policy_denied", text)


if __name__ == "__main__":
    unittest.main()
