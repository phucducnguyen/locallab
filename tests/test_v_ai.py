"""Lane V: the AI-at-work panel: live GB10 numbers, pending/explained, rule vs AI, citations."""

import http.server
import json
import os
import re
import stat
import threading
import time
import unittest

from bench import dashboard
from stub.db import Writer
from tests.test_v_dashboard import Base, add_health, ok_fields

DIAG_HW = "DIAGNOSIS: HARDWARE | VERDICT: HEALTH_FAIL | COMMIT: none | CAUSE: sensor-open"
DIAG_CODE = "DIAGNOSIS: CODE | VERDICT: HARDFAULT | COMMIT: abc1234 | CAUSE: new-code"


class MetricsServer(http.server.HTTPServer):
    def __init__(self):
        self.count = 0
        super().__init__(("127.0.0.1", 0), MetricsHandler)


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.count += 1
        n = self.server.count
        body = ("# HELP vllm:generation_tokens_total Number of generation tokens.\n"
                "# TYPE vllm:generation_tokens_total counter\n"
                'vllm:generation_tokens_total{model_name="m",engine="0"} %d.0\n'
                'vllm:generation_tokens_total{model_name="m",engine="1"} %d.0\n'
                'vllm:generation_tokens_created{model_name="m"} 1.7e9\n'
                "vllm:prompt_tokens_total 999.0\n" % (100 * n, 50 * n)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ProbeTest(Base):
    def fake_smi(self, out="87, 95.30", rc=0):
        p = os.path.join(self.tmp.name, "nvidia-smi")
        with open(p, "w") as f:
            f.write("#!/bin/sh\necho '%s'\nexit %d\n" % (out, rc))
        os.chmod(p, stat.S_IRWXU)
        return p

    def metrics(self):
        srv = MetricsServer()
        th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
        th.start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return srv, "http://127.0.0.1:%d/metrics" % srv.server_address[1]

    def test_parsers(self):
        self.assertEqual(dashboard.parse_gpu("87, 95.30\n"), (87.0, 95.3))
        self.assertEqual(dashboard.parse_gpu("45, [N/A]"), (45.0, None))
        self.assertEqual(dashboard.parse_gpu(""), (None, None))
        self.assertIsNone(dashboard.generation_tokens("# nothing\nother 5\n"))
        self.assertEqual(dashboard.generation_tokens(
            "vllm:generation_tokens_total 3\nvllm:generation_tokens_total{a=\"b\"} 4.5\n"), 7.5)

    def test_rising_counter_and_fake_smi(self):
        srv, url = self.metrics()
        p = dashboard.Probe(nvidia_smi=self.fake_smi(), url=url)
        d = p.poll_once(now=1000.0)
        self.assertEqual((d["gpu_util"], d["power_w"]), (87.0, 95.3))
        self.assertEqual(d["tokens"], 150)
        self.assertIsNone(d["tok_s"])
        d = p.poll_once(now=1002.0)  # +150 tokens in 2 s
        self.assertEqual(d["tokens"], 300)
        self.assertEqual(d["tok_s"], 75.0)

    def test_env_overrides_and_thread(self):
        srv, url = self.metrics()
        old = {k: os.environ.get(k) for k in ("LW_NVIDIA_SMI", "LW_VLLM_METRICS")}
        os.environ["LW_NVIDIA_SMI"] = self.fake_smi("12, 40.0")
        os.environ["LW_VLLM_METRICS"] = url
        try:
            p = dashboard.Probe(interval=0.1).start()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        deadline = time.time() + 3
        while time.time() < deadline and p.snapshot()["tok_s"] is None:
            time.sleep(0.05)
        p.stop()
        d = p.snapshot()
        self.assertEqual(d["gpu_util"], 12.0)
        self.assertGreater(d["tok_s"], 0)
        self.assertIn(url.split("/")[2], dashboard.badge(url))

    def test_failures_show_nothing(self):
        p = dashboard.Probe(nvidia_smi=os.path.join(self.tmp.name, "missing"),
                            url="http://127.0.0.1:9/metrics", timeout=0.5)
        d = p.poll_once()
        self.assertEqual((d["gpu_util"], d["tok_s"], d["tokens"]), (None, None, None))
        p = dashboard.Probe(nvidia_smi=self.fake_smi("x", rc=3), url="http://127.0.0.1:9/m",
                            timeout=0.5)
        self.assertIsNone(p.poll_once()["gpu_util"])

    def test_counter_reset_is_zero(self):
        srv, url = self.metrics()
        p = dashboard.Probe(nvidia_smi=self.fake_smi(), url=url)
        p.poll_once(now=10.0)
        p.poll_once(now=11.0)
        srv.count = 0
        self.assertEqual(p.poll_once(now=12.0)["tok_s"], 0.0)


def add_explain(conn, iid, text, t, full=None):
    conn.execute("INSERT INTO alerts (incident_id, transition, text, created, sent) "
                 "VALUES (?,?,?,?,?)", (iid, "explain", text, t, t + 0.3))
    if full is not None:
        ev = json.loads(conn.execute("SELECT evidence FROM incidents WHERE id=?",
                                     (iid,)).fetchone()[0] or "{}")
        ev["explanation"] = full
        conn.execute("UPDATE incidents SET evidence=? WHERE id=?", (json.dumps(ev), iid))
    conn.commit()


class AiStateTest(Base):
    def setup_incident(self, triage="HARDWARE", kind="HEALTH_FAIL", ago=10.0):
        conn = self.empty()
        f, line = ok_fields(sensor="OPEN")
        add_health(conn, time.time(), line, **f)
        t = time.time() - ago
        iid = Writer(conn).open_incident(t, kind, triage, "x",
                                         {"device": "sensor", "fields": {"sensor": "OPEN"}},
                                         t_first_bad=t - 0.5)
        conn.execute("UPDATE incidents SET t_alert_sent=? WHERE id=?", (t + 1.1, iid))
        conn.commit()
        return conn, iid, t

    def test_idle(self):
        conn = self.empty()
        f, line = ok_fields()
        add_health(conn, time.time(), line, **f)
        conn.close()
        ai = self.state()["ai"]
        self.assertEqual(ai["phase"], "idle")
        self.assertEqual(ai["model"], "Qwen3.6-35B-A3B")
        self.assertIn("No cloud AI calls", ai["badge"])
        self.assertIsNone(ai["live"]["gpu_util"])

    def test_pending(self):
        conn, iid, t = self.setup_incident()
        conn.close()
        ai = self.state()["ai"]
        self.assertEqual(ai["phase"], "pending")
        self.assertEqual(ai["incident"], iid)
        self.assertAlmostEqual(ai["elapsed_s"], 8.9, delta=0.5)
        self.assertEqual(ai["rule"]["status"], "working")
        self.assertEqual(ai["rule"]["rule_route"], "Hardware team")
        self.assertEqual(ai["citations"], [])

    def test_pending_token_counter_rises(self):
        conn, iid, t = self.setup_incident()
        conn.close()
        d = dashboard.Dashboard(self.path)

        class P:
            url = "http://127.0.0.1:8000/metrics"
            n = 1000

            def snapshot(self):
                return {"gpu_util": 90.0, "power_w": 100.0, "tok_s": 40.0, "tokens": self.n}
        d.probe = P()
        try:
            self.assertEqual(d.state()["ai"]["tokens_run"], 0)
            d.probe.n = 1240
            ai = d.state()["ai"]
        finally:
            d.db.close()
        self.assertEqual(ai["tokens_run"], 240)
        self.assertEqual(ai["live"]["tok_s"], 40.0)

    def test_explained_agrees(self):
        conn, iid, t = self.setup_incident()
        full = ("The sensor wire reads sensor=OPEN [serial 10:00:00.000]. It is likely "
                "unplugged [incident %d]. More detail.\n%s" % (iid, DIAG_HW))
        alert = "\U0001f9e0 #%d: The sensor wire reads sensor=OPEN [serial 10:00:00.000]. " \
                "It is likely unplugged.\n%s" % (iid, DIAG_HW)
        add_explain(conn, iid, alert, t + 25.0, full)
        conn.close()
        ai = self.state()["ai"]
        self.assertEqual(ai["phase"], "done")
        self.assertEqual(ai["answer"], "The sensor wire reads sensor=OPEN. It is likely "
                                       "unplugged.")
        self.assertAlmostEqual(ai["explain_s"], 24.2, delta=0.05)
        r = ai["rule"]
        self.assertEqual((r["rule"], r["ai"], r["cause"], r["verdict"]),
                         ("HARDWARE", "HARDWARE", "sensor-open", "agrees"))
        self.assertEqual([c["kind"] for c in ai["citations"]], ["serial", "incident"])
        self.assertTrue(ai["citations"][0]["line"].startswith("LW HEALTH 7"))

    def test_explained_differs(self):
        conn, iid, t = self.setup_incident()
        add_explain(conn, iid, "\U0001f9e0 #%d: The code changed.\n%s" % (iid, DIAG_CODE),
                    t + 20.0)
        conn.close()
        r = self.state()["ai"]["rule"]
        self.assertEqual((r["rule"], r["ai"], r["verdict"]), ("HARDWARE", "CODE", "differs"))

    def test_no_diagnosis_and_unavailable(self):
        conn, iid, t = self.setup_incident()
        add_explain(conn, iid, "\U0001f9e0 #%d: something.\nDIAGNOSIS: maybe hardware" % iid,
                    t + 20.0)
        conn.close()
        ai = self.state()["ai"]
        self.assertEqual(ai["rule"]["status"], "none")
        self.assertIsNone(ai["rule"]["verdict"])
        conn, iid2, t = self.setup_incident()
        add_explain(conn, iid2, "\U0001f9e0 #%d: explanation unavailable; triage stands"
                    % iid2, t + 60.0)
        conn.close()
        ai = self.state()["ai"]
        self.assertEqual((ai["incident"], ai["phase"]), (iid2, "unavailable"))
        self.assertIsNone(ai["answer"])
        self.assertEqual(ai["rule"]["status"], "none")

    def test_late_stops_analysing(self):
        conn, iid, t = self.setup_incident(ago=dashboard.AI_LATE_S + 5)
        conn.close()
        self.assertEqual(self.state()["ai"]["phase"], "late")


class CitationTest(unittest.TestCase):
    def test_good(self):
        text = ("A [serial 12:00:01.250]. B [src/main.c:42; diff abc1234 +3]. "
                "C [incident 7] D [regx BITA]. E [serial 12:00:01.250] F [serial 1:2:3.4]")
        cs = dashboard.parse_citations(text)
        self.assertEqual([(c["kind"], c["ref"]) for c in cs],
                         [("serial", "12:00:01.250"), ("file", "src/main.c:42"),
                          ("diff", "abc1234 +3"), ("incident", "7")])

    def test_malformed_and_none(self):
        self.assertEqual(dashboard.parse_citations(
            "x [serial 12:00:01] [diff XYZ +1] [incident seven] [see above] [main.c]"), [])
        self.assertEqual(dashboard.parse_citations("no brackets here."), [])
        self.assertEqual(dashboard.parse_citations(None), [])
        cs = dashboard.parse_citations("[regx BITA] [serial 01:02:03.004 extra]")
        self.assertEqual([c["kind"] for c in cs], ["bit"])

    def test_diagnosis_exact(self):
        self.assertEqual(dashboard.find_diagnosis("x\n**%s**" % DIAG_HW)["cause"],
                         "sensor-open")
        self.assertIsNone(dashboard.find_diagnosis("DIAGNOSIS: HARDWARE | VERDICT: x"))
        self.assertIsNone(dashboard.find_diagnosis(None))
        r = dashboard.rule_vs_ai("UNKNOWN", None, dashboard.find_diagnosis(DIAG_HW), True)
        self.assertEqual((r["verdict"], r["rule_route"]), ("differs", "Triage"))


class HighlightAskTest(AiStateTest):
    def test_highlight_exact_only(self):
        h = dashboard.highlights
        self.assertEqual(h("The wire on A2-A3 is loose; see B1 and PC13. 0xA0 BA1 A6", None),
                         ["A2", "A3", "B1", "PC13"])
        self.assertEqual(h("nothing named", "sensor-open"), ["sensor"])
        self.assertEqual(h("nothing named", "new-code"), [])
        self.assertEqual(h(None, None), [])

    def test_highlight_only_when_explained(self):
        conn, iid, t = self.setup_incident()
        conn.close()
        self.assertEqual(self.state()["ai"]["highlight"], [])
        conn = __import__("stub.db", fromlist=["connect"]).connect(self.path)
        add_explain(conn, iid, "\U0001f9e0 #%d: Pin A2 reads open.\n%s" % (iid, DIAG_HW),
                    t + 20.0)
        conn.close()
        self.assertEqual(self.state()["ai"]["highlight"], ["A2", "sensor"])

    def asks(self, rows):
        import sqlite3
        c = sqlite3.connect(os.path.join(self.tmp.name, "asks.db"))
        c.execute("CREATE TABLE asks (id INTEGER PRIMARY KEY, t_asked REAL, question TEXT, "
                  "incident_id INTEGER, answer TEXT, t_answered REAL, status TEXT)")
        c.executemany("INSERT INTO asks (t_asked, question, incident_id, answer, t_answered, "
                      "status) VALUES (?,?,?,?,?,?)", rows)
        c.commit()
        c.close()

    def test_ask_absent_then_newest(self):
        self.empty().close()
        self.assertIsNone(self.state()["ai"]["ask"])
        now = time.time()
        self.asks([(now - 50, "old one?", None, "Old.", now - 40, "answered"),
                   (now - 4, "why is it red?", 1, None, None, "asked")])
        ask = self.state()["ai"]["ask"]
        self.assertEqual((ask["question"], ask["status"]), ("why is it red?", "asked"))
        self.assertAlmostEqual(ask["elapsed_s"], 4, delta=1)

    def test_ask_answered_and_bad_table(self):
        self.empty().close()
        now = time.time()
        self.asks([(now - 9, "what failed?", None, "The sensor wire is open [incident 1]. "
                    "Check it. Third sentence.", now - 2, "answered")])
        ask = self.state()["ai"]["ask"]
        self.assertEqual(ask["answer"], "The sensor wire is open. Check it.")
        self.assertIsNone(ask["elapsed_s"])
        os.remove(os.path.join(self.tmp.name, "asks.db"))
        import sqlite3
        sqlite3.connect(os.path.join(self.tmp.name, "asks.db")).close()
        self.assertIsNone(self.state()["ai"]["ask"])


class PageTest(unittest.TestCase):
    def test_no_external_urls(self):
        html = dashboard.load_page()
        self.assertEqual(re.findall(r"(?i)\b(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", html), [])
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("@import", html)
        self.assertNotIn("0 bytes", html)
        for word in ("aipanel", "Rule vs AI", "The rule decides; the AI explains.",
                     "GB10 now", "What the AI looked at", "pipeline"):
            self.assertIn(word, html)


if __name__ == "__main__":
    unittest.main()
