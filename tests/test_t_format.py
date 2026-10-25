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


def lines_of(t):
    ls = t.splitlines()
    assert 1 <= len(ls) <= 3, t
    return ls


class OpenAlert(unittest.TestCase):
    def check(self, row, head, team):
        ls = lines_of(alerts.alert_text("open", row, CITE))
        self.assertEqual(ls, [head, team] if team else [head])
        if not team:
            return
        for x in ("Board 1", "(#7)", "Team: ", "/ask 7"):
            self.assertIn(x, "\n".join(ls))
        for tech in ("HardFault", "loopback", "vdd", "sha", "LW "):
            self.assertNotIn(tech, ls[0])
        before = ls[1].split("Details:")[0]
        self.assertNotIn("mV", before)

    def test_loopback(self):
        self.check(inc(ev={"facts": {"loopback": ["OPEN", "OPEN"]}}),
                   "🔴 Board 1: the test wire came loose. (#7)",
                   "Team: hardware. Not a code problem. Ask: /ask 7")

    def test_sensor_open(self):
        self.check(inc(ev={"device": "sensor", "pins": "A2-A3", "facts": {"sensor": ["OPEN"]}}),
                   "🔴 Board 1: the sensor wire came loose. (#7)",
                   "Team: hardware. Not a code problem. Ask: /ask 7")

    def test_radio_stuck(self):
        self.check(inc(ev={"device": "radio", "pins": "A4-A5", "facts": {"state": "STUCK"}}),
                   "🔴 Board 1: the radio wire is stuck to ground. (#7)",
                   "Team: hardware. Not a code problem. Ask: /ask 7")

    def test_power_low_and_bor(self):
        self.check(inc(ev={"device": "power", "facts": {"cause": "VDD_LOW", "vdd": [2990, 2950]}}),
                   "🔴 Board 1 is not getting enough power. (#7)",
                   "Team: hardware. Not a code problem. Ask: /ask 7")
        self.check(inc(ev={"device": "power", "facts": {"cause": "BOR"}}),
                   "🔴 Board 1 restarted because its power dropped. (#7)",
                   "Team: hardware. Not a code problem. Ask: /ask 7")

    def test_estop(self):
        self.check(inc(kind="ESTOP"),
                   "⛔ Board 1: someone pressed the stop button. Not a fault. (#7)", None)

    def test_hardfault_with_file_line(self):
        ev = {"file_line": "src/widget.c:42", "addr2line": {"function": "widget_poll"},
              "fault": {"summary": "HFSR FORCED; UFSR UNDEFINSTR; pc=0x08000200"}}
        self.check(inc(kind="HARDFAULT", triage="CODE", route="Firmware team", ev=ev),
                   "🔴 Board 1: the new code made it crash. (#7)",
                   "Team: firmware. Details: src/widget.c:42. Ask: /ask 7")

    def test_hardfault_without_file_line(self):
        self.check(inc(kind="HARDFAULT", triage="CODE", route="Firmware team"),
                   "🔴 Board 1: the new code made it crash. (#7)",
                   "Team: firmware. Ask: /ask 7")

    def test_run_fail(self):
        self.check(inc(kind="RUN_FAIL", triage="CODE", route="Firmware team",
                       ev={"facts": {"verdict": "FAIL"}}),
                   "🔴 Board 1: the new code failed its tests. (#7)",
                   "Team: firmware. Ask: /ask 7")

    def test_unknown_routes_to_triage(self):
        self.check(inc(kind="RUN_FAIL", triage="UNKNOWN", route=None,
                       ev={"facts": {"verdict": "NO_OUTPUT"}}),
                   "🟡 Board 1: it stopped talking after the update. Checking if it is the "
                   "board or the code. (#7)",
                   "Team: triage. Ask: /ask 7")

    def test_other_board_name(self):
        t = alerts.alert_text("open", inc(board="bench", kind="SILENT"))
        self.assertTrue(t.startswith("🔴 bench: it stopped talking. (#7)"))


class OtherRows(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(alerts.alert_text("resolve", inc(ev={"device": "sensor"})),
                         "✅ Board 1: the sensor wire is back. Fixed after 9 seconds. (#7)")
        self.assertEqual(alerts.alert_text("resolve", inc(kind="ESTOP")),
                         "✅ Board 1: the stop button was released after 9 seconds. (#7)")
        self.assertEqual(alerts.alert_text("resolve", inc(kind="HARDFAULT")),
                         "✅ Board 1 is working again. Fixed after 9 seconds. (#7)")

    def test_flap(self):
        self.assertEqual(alerts.alert_text("flap-2", inc(ev={"facts": {"loopback": ["OPEN"]}})),
                         "🟠 Board 1: the test wire keeps coming and going. Probably loose. (#7)")
        self.assertEqual(alerts.alert_text("flap-3", inc(kind="HARDFAULT", flaps=3)),
                         "🟠 Board 1: the problem keeps coming and going (3 times). (#7)")

    def test_diag(self):
        t = alerts.diag_text(7, "test", [{"raw_ts": "T4.20",
                                          "line": "LW TEST loopback FAIL pa1=high"}], 3)
        self.assertEqual(t, "🔧 The board ran its own self-check: it failed. (#7)")
        self.assertEqual(alerts.diag_text(7, "test", [{"raw_ts": "1", "line": "LW TEST ok"}], 3),
                         "🔧 The board ran its own self-check: it passed. (#7)")
        self.assertEqual(alerts.diag_text(7, "test", [], 3),
                         "🔧 The board did not answer its self-check. (#7)")

    def test_decode(self):
        self.assertEqual(alerts.decode_text(7, {"file_line": "src/a.c:9"}),
                         "🧩 Found where the code crashed. (#7)\nDetails: src/a.c:9")
        self.assertIn("(#7)", alerts.decode_text(7, {}))

    def test_confirm(self):
        class R:
            status, triage, route, reason = "CONFIRMED", "CODE", "Firmware team", "r " * 200
            prior_triage, last_pass_sha, reflash_verdict, output_returned = (
                "UNKNOWN", "def5678", "PASS", True)
        self.assertEqual(confirm.confirm_alert_text(7, inc(), R()),
                         "✔ Board 1: checked by putting the last good code back. It is the "
                         "code, not the board. (#7)\nTeam: firmware. Ask: /ask 7")
        R.status, R.route = "NOT_CONFIRMED", "Triage"
        self.assertEqual(confirm.confirm_alert_text(7, inc(), R()),
                         "✖ Board 1: the check could not tell if it is the board or the "
                         "code. (#7)\nTeam: triage. Ask: /ask 7")


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
        self.assertEqual(lines[0], "🧠 #7: The jumper between A0 and A1 "
                                   "is unplugged [T3.01]. The firmware drives PA0 as before.")
        self.assertEqual(lines[1], "Next: Reseat the A0-A1 jumper and watch for "
                                   "loopback=OK.")
        self.assertEqual(lines[2], "DIAGNOSIS: HARDWARE loopback wire open [T3.01]")

    def test_bold_labels(self):
        text = ("**Likely cause:** `div` by zero in `widget_poll`. Seen at [12:00]. More.\n"
                "**Next step:** Guard the divisor.\nDIAGNOSIS: CODE divide by zero")
        lines = alerts.explain_text(3, text).splitlines()
        self.assertEqual(lines, ["🧠 #3: div by zero in widget_poll. "
                                 "Seen at [12:00].", "Next: Guard the divisor.",
                                 "DIAGNOSIS: CODE divide by zero"])

    def test_no_sections(self):
        text = ("The sensor link reads OPEN. The wire on A2-A3 is loose. It flapped twice.\n"
                "DIAGNOSIS: HARDWARE sensor wire")
        lines = alerts.explain_text(4, text).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "🧠 #4: The sensor link reads OPEN. "
                                   "The wire on A2-A3 is loose.")
        self.assertEqual(lines[1], "DIAGNOSIS: HARDWARE sensor wire")

    def test_no_diagnosis(self):
        self.assertEqual(alerts.explain_text(5, "some text\nno verdict"),
                         "🧠 #5: explanation unavailable; triage stands")
        self.assertEqual(alerts.explain_text(5, None),
                         "🧠 #5: explanation unavailable; triage stands")

    def test_long_citations_dropped(self):
        cites = " ".join("[T%d.%02d]" % (i, i) for i in range(40))
        text = ("## Likely cause\nThe radio input is stuck low %s. It never toggles %s.\n"
                "DIAGNOSIS: HARDWARE radio stuck" % (cites, cites))
        lines = alerts.explain_text(6, text).splitlines()
        self.assertEqual(lines[0], "🧠 #6: The radio input is stuck low. "
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
