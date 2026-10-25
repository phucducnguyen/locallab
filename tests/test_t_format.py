"""Lane T: short Telegram wording for every alert kind, and the explain condenser."""

import json
import unittest

from bench import alerts, confirm


def inc(kind="HEALTH_FAIL", triage="HARDWARE", route="Hardware team", ev=None, **kw):
    row = {"id": 7, "board": "nucleo-1", "kind": kind, "triage": triage, "route": route,
           "reason": "two loopback=OPEN lines in a row on a verified build",
           "firmware": "abc1234", "t_open": 103.5, "t_first_bad_line": 101.0,
           "evidence": json.dumps(ev or {}), "duration_s": 9.4, "flaps": 2}
    row.update(kw)
    return row


CITE = {"raw_ts": "T3.01", "line": "LW HEALTH 2 loopback=OPEN"}


class OpenAlert(unittest.TestCase):
    def check(self, row, cite=CITE, what=None, plain=None):
        text = alerts.alert_text("open", row, cite)
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 6)
        details = [x for x in lines if x.startswith("Details: ")][0]
        for must in ("#7", row["triage"], row["board"], "fw %s" % row["firmware"]):
            self.assertIn(must, details)
        if cite:
            q = [x for x in lines if x.startswith("Line: ")][0]
            self.assertIn(cite["line"], q)
            self.assertIn("[%s]" % cite["raw_ts"], q)
        self.assertIn("Alert 2.5 s after the fault.", text)
        if what:
            self.assertTrue(details.startswith("Details: " + what), details)
        if plain:
            self.assertEqual(lines[0], plain)
        return lines

    def test_loopback(self):
        lines = self.check(inc(ev={"facts": {"loopback": ["OPEN", "OPEN"]}}),
                           what="loopback OPEN (A0–A1 wire) · nucleo-1 · fw abc1234",
                           plain="🔴 Problem on test board 1: the test wire (A0–A1) is "
                                 "disconnected.")
        self.assertEqual(lines[1], "Who: Hardware team · Not a software bug.")
        self.assertEqual(len(lines), 5)   # HARDWARE: no Why line

    def test_sensor_open(self):
        self.check(inc(ev={"device": "sensor", "pins": "A2-A3", "facts": {"sensor": ["OPEN"]}}),
                   what="sensor link OPEN (A2–A3 wire)",
                   plain="🔴 Problem on test board 1: the sensor wire (A2–A3) is disconnected.")

    def test_radio_stuck(self):
        self.check(inc(ev={"device": "radio", "pins": "A4-A5", "facts": {"state": "STUCK"}}),
                   what="radio link STUCK (A5 to ground)",
                   plain="🔴 Problem on test board 1: the radio wire (A4–A5) is shorted to "
                         "ground.")

    def test_power_low_and_bor(self):
        self.check(inc(ev={"device": "power", "facts": {"cause": "VDD_LOW", "vdd": [2990, 2950]}}),
                   what="power low (vdd 2950 mV)",
                   plain="🔴 Test board 1 is getting too little power (2.95 V).")
        self.check(inc(ev={"device": "power", "facts": {"cause": "BOR"}}),
                   what="power brown-out reset (BOR)")

    def test_estop(self):
        lines = self.check(inc(kind="ESTOP"), what="emergency stop pressed (operator action)",
                           plain="⛔ Someone pressed the emergency stop on test board 1. "
                                 "Not a fault.")
        self.assertFalse(any(x.startswith("Who:") for x in lines))

    def test_hardfault_with_file_line(self):
        ev = {"file_line": "src/widget.c:42", "addr2line": {"function": "widget_poll"},
              "fault": {"summary": "HFSR FORCED; UFSR UNDEFINSTR; pc=0x08000200; lr=0x08000151"}}
        lines = self.check(inc(kind="HARDFAULT", triage="CODE", route="Firmware team", ev=ev),
                           cite={"raw_ts": "12:00:00.000", "line": "LW HARDFAULT pc=08000200"},
                           what="HardFault at src/widget.c:42 · HFSR FORCED; UFSR UNDEFINSTR "
                                "in widget_poll()",
                           plain="🔴 Problem on test board 1: the firmware crashed.")
        self.assertEqual(lines[1], "Who: Firmware team · Caused by the latest code change "
                                   "(abc1234).")
        self.assertEqual(len(lines), 6)   # CODE keeps the Why line
        self.assertTrue(lines[5].startswith("Why: "))
        self.assertNotIn("pc=0x", lines[2])

    def test_hardfault_without_file_line(self):
        self.check(inc(kind="HARDFAULT", triage="CODE", route="Firmware team"),
                   cite={"raw_ts": "1.0", "line": "LW HARDFAULT"},
                   what="HardFault (no file:line) · nucleo-1")

    def test_run_fail(self):
        lines = self.check(inc(kind="RUN_FAIL", triage="CODE", route="Firmware team",
                               ev={"facts": {"verdict": "FAIL"}}),
                           cite={"raw_ts": "T9", "line": "LW RESULT FAIL 3/4"},
                           what="FAIL on abc1234 · nucleo-1 · fw abc1234",
                           plain="🔴 Problem on test board 1: the firmware failed its tests.")
        self.assertIn("loopback=OPEN lines", lines[5])

    def test_unknown_routes_to_triage(self):
        row = inc(triage="UNKNOWN", route=None, reason="x" * 400,
                  ev={"facts": {"loopback": ["STUCK"]}})
        lines = self.check(row, what="loopback STUCK (A1 to ground)")
        self.assertTrue(lines[0].startswith("🟡 Problem on test board 1: the test wire (A0–A1) "
                                            "is shorted to ground. Checking whether"))
        self.assertEqual(lines[1], "Who: Triage")
        self.assertLessEqual(len(lines[5]), 150)

    def test_no_cite_no_timing(self):
        row = inc(kind="LINK_DOWN", t_first_bad_line=None)
        lines = alerts.alert_text("open", row, None).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("serial link down", lines[2])

    def test_long_line_trimmed(self):
        cite = {"raw_ts": "T1", "line": "LW HEALTH " + "z" * 300}
        q = alerts.alert_text("open", inc(), cite).splitlines()[3]
        self.assertLess(len(q), 110)
        self.assertTrue(q.endswith("  [T1]"))


class OtherRows(unittest.TestCase):
    def test_resolve(self):
        t = alerts.alert_text("resolve", inc(ev={"device": "sensor", "pins": "A2-A3"}))
        self.assertEqual(t, "✅ Fixed: test board 1's sensor wire (A2–A3) is back to normal "
                            "(after 9 s).\n#7 · sensor link (A2–A3) resolved")
        t = alerts.alert_text("resolve", inc(kind="ESTOP"))
        self.assertTrue(t.startswith("✅ Cleared: the emergency stop on test board 1 was "
                                     "released"))

    def test_flap(self):
        t = alerts.alert_text("flap-2", inc(ev={"facts": {"loopback": ["OPEN"]}}))
        self.assertEqual(t, "🟠 Test board 1's test wire (A0–A1) keeps connecting and "
                            "disconnecting (2 times). Probably loose.\n"
                            "#7 · loopback (A0–A1) flapping")
        t = alerts.alert_text("flap-1", inc(kind="HARDFAULT", ev={"file_line": "a.c:3"}))
        self.assertIn("HardFault at a.c:3", t)

    def test_diag(self):
        t = alerts.diag_text(7, "test", [{"raw_ts": "T4.20",
                                          "line": "LW TEST loopback FAIL pa1=high"}], 3)
        self.assertEqual(t, "🔧 The board's own self-test failed (#7).\n"
                            "Details: LW TEST loopback FAIL pa1=high  [T4.20]")
        self.assertIn("no reply to 'test' within 3 s", alerts.diag_text(7, "test", [], 3))

    def test_decode(self):
        t = alerts.decode_text(7, {"file_line": "src/a.c:9",
                                   "fault": {"summary": "UFSR DIVBYZERO; pc=0x1"}})
        self.assertEqual(t, "🧩 Found where the firmware crashed (#7): src/a.c:9\n"
                            "Details: HardFault at src/a.c:9 · UFSR DIVBYZERO")

    def test_confirm(self):
        class R:
            status, triage, route, reason = "CONFIRMED", "CODE", "Firmware team", "r " * 200
            prior_triage, last_pass_sha, reflash_verdict, output_returned = (
                "UNKNOWN", "def5678", "PASS", True)
        lines = confirm.confirm_alert_text(7, inc(), R()).splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "✔ Confirmed by reflashing the last good firmware on test "
                                   "board 1 (#7): it is the code, not the board.")
        self.assertEqual(lines[1], "Who: Firmware team")
        self.assertIn("triage UNKNOWN → CODE · reflash def5678 → PASS", lines[2])
        self.assertLess(len(lines[3]), 150)


SECTIONS = """## Summary
The loopback wire on A0-A1 reads open on every HEALTH line.

## Likely cause
**The jumper between A0 and A1 is unplugged** [T3.01]. The firmware drives PA0 as before.
A third sentence that should not appear.

## Next step
Reseat the A0-A1 jumper and watch for loopback=OK. Then rerun.

DIAGNOSIS: HARDWARE loopback wire open [T3.01]
"""


class Explain(unittest.TestCase):
    def test_sections(self):
        t = alerts.explain_text(7, SECTIONS)
        lines = t.splitlines()
        self.assertLessEqual(len(lines), 6)
        self.assertEqual(lines[0], "🧠 What the AI found (#7): The jumper between A0 and A1 "
                                   "is unplugged [T3.01]. The firmware drives PA0 as before.")
        self.assertEqual(lines[1], "Next step: Reseat the A0-A1 jumper and watch for "
                                   "loopback=OK.")
        self.assertEqual(lines[2], "DIAGNOSIS: HARDWARE loopback wire open [T3.01]")

    def test_bold_labels(self):
        text = ("**Likely cause:** `div` by zero in `widget_poll`. Seen at [12:00]. More.\n"
                "**Next step:** Guard the divisor.\nDIAGNOSIS: CODE divide by zero")
        lines = alerts.explain_text(3, text).splitlines()
        self.assertEqual(lines, ["🧠 What the AI found (#3): div by zero in widget_poll. "
                                 "Seen at [12:00].", "Next step: Guard the divisor.",
                                 "DIAGNOSIS: CODE divide by zero"])

    def test_no_sections(self):
        text = ("The sensor link reads OPEN. The wire on A2-A3 is loose. It flapped twice.\n"
                "DIAGNOSIS: HARDWARE sensor wire")
        lines = alerts.explain_text(4, text).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "🧠 What the AI found (#4): The sensor link reads OPEN. "
                                   "The wire on A2-A3 is loose.")
        self.assertEqual(lines[1], "DIAGNOSIS: HARDWARE sensor wire")

    def test_no_diagnosis(self):
        self.assertEqual(alerts.explain_text(5, "some text\nno verdict"),
                         "🧠 What the AI found (#5): explanation unavailable; triage stands")
        self.assertEqual(alerts.explain_text(5, None),
                         "🧠 What the AI found (#5): explanation unavailable; triage stands")

    def test_long_citations_dropped(self):
        cites = " ".join("[T%d.%02d]" % (i, i) for i in range(40))
        text = ("## Likely cause\nThe radio input is stuck low %s. It never toggles %s.\n"
                "DIAGNOSIS: HARDWARE radio stuck" % (cites, cites))
        lines = alerts.explain_text(6, text).splitlines()
        self.assertEqual(lines[0], "🧠 What the AI found (#6): The radio input is stuck low. "
                                   "It never toggles.")
        self.assertLessEqual(len(lines), 6)

    def test_long_answer_line_count(self):
        body = "\n".join("## Section %d\n%s" % (i, "word " * 200) for i in range(30))
        t = alerts.explain_text(8, body + "\n## Likely cause\n" + "long " * 500 +
                                "\nDIAGNOSIS: UNKNOWN")
        self.assertLessEqual(len(t.splitlines()), 6)
        self.assertLess(len(t), 800)


if __name__ == "__main__":
    unittest.main()
