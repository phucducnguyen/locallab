"""Lane A: the fixed triage rule, one test per branch, and routing.

Shas and lines are invented; detail values are made up.
"""

import unittest

from bench.triage import PassHistory, judge_incident, judge_run, route_for

SHA = "abcdef1"
NEW = "fedcba9"


def cite(line):
    return {"event_id": 1, "raw_ts": "12:00:00.000", "line": line}


BAD = [cite("LW HEALTH 7 loopback=OPEN"), cite("LW HEALTH 8 loopback=OPEN")]


class TestRoutes(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(route_for("HARDWARE"), "Hardware team")
        self.assertEqual(route_for("CODE"), "Firmware team")
        self.assertEqual(route_for("UNKNOWN"), "Triage")
        self.assertIsNone(route_for("NONE"))


class TestHistory(unittest.TestCase):
    def test_passed_and_last(self):
        h = PassHistory([(SHA, 10.0), (NEW, 20.0)])
        self.assertEqual(h.passed(SHA), 10.0)
        self.assertEqual(h.passed("abcdef1234567"), 10.0)   # prefix of a long sha
        self.assertIsNone(h.passed("0000000"))
        self.assertEqual(h.last(), (NEW, 20.0))
        self.assertEqual(h.last(exclude=NEW), (SHA, 10.0))
        self.assertEqual(PassHistory().last(), (None, None))


class TestIncidentRule(unittest.TestCase):
    def test_health_fail_on_a_passing_sha_is_hardware(self):
        tri, why = judge_incident("HEALTH_FAIL", {}, BAD, SHA, PassHistory([(SHA, 0.0)]))
        self.assertEqual(tri, "HARDWARE")
        self.assertIn(SHA, why)
        self.assertIn('"LW HEALTH 7 loopback=OPEN"', why)
        self.assertIn("no reflash since", why)
        self.assertTrue(why.endswith("."))
        self.assertEqual(why.count(". "), 0)  # one sentence

    def test_health_fail_unverified_is_unknown(self):
        tri, why = judge_incident("HEALTH_FAIL", {}, BAD, None, PassHistory([(SHA, 0.0)]))
        self.assertEqual(tri, "UNKNOWN")
        self.assertIn("No verified sha", why)

    def test_health_fail_on_new_sha_first_run_is_unknown(self):
        tri, why = judge_incident("HEALTH_FAIL", {}, BAD, NEW, PassHistory([(SHA, 0.0)]))
        self.assertEqual(tri, "UNKNOWN")
        self.assertIn("confined to the loopback", why)
        self.assertIn(SHA, why)  # names the last passing sha to reflash

    def test_link_down(self):
        self.assertEqual(judge_incident("LINK_DOWN", {}, [], SHA, PassHistory())[0], "HARDWARE")
        self.assertEqual(judge_incident("LINK_DOWN", {}, [], None, PassHistory())[0], "UNKNOWN")

    def test_reboot_reset_pin_on_passing_sha_is_hardware(self):
        c = [cite("LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)]
        tri, why = judge_incident("REBOOT", {"reset": ["PIN"]}, c, SHA, PassHistory([(SHA, 1.0)]))
        self.assertEqual(tri, "HARDWARE")
        self.assertIn("reset=PIN", why)

    def test_reboot_pin_on_sha_without_pass_is_unknown(self):
        c = [cite("LW BOOT fw=%s reset=PIN csr=0x04000000" % NEW)]
        self.assertEqual(judge_incident("REBOOT", {"reset": ["PIN"]}, c, NEW,
                                        PassHistory([(SHA, 1.0)]))[0], "UNKNOWN")

    def test_reboot_with_watchdog_flag_is_not_the_button(self):
        c = [cite("LW BOOT fw=%s reset=IWDG,PIN csr=0x24000000" % SHA)]
        tri, why = judge_incident("REBOOT", {"reset": ["IWDG", "PIN"]}, c, SHA,
                                  PassHistory([(SHA, 1.0)]))
        self.assertEqual(tri, "UNKNOWN")
        self.assertIn("IWDG,PIN", why)

    def test_reboot_from_counter_is_unknown(self):
        tri, _ = judge_incident("REBOOT", {"reset": None, "before": 40, "after": 1}, [], SHA,
                                PassHistory([(SHA, 1.0)]))
        self.assertEqual(tri, "UNKNOWN")

    def test_hardfault_is_code_on_verified_sha(self):
        c = [cite("LW HARDFAULT pc=0x1 lr=0x2")]
        tri, why = judge_incident("HARDFAULT", {}, c, SHA, PassHistory())
        self.assertEqual(tri, "CODE")
        self.assertIn('"LW HARDFAULT pc=0x1 lr=0x2"', why)
        self.assertEqual(judge_incident("HARDFAULT", {}, c, None, PassHistory())[0], "UNKNOWN")

    def test_silent_is_unknown_until_confirmed(self):
        c = [cite("LW ALIVE 3")]
        self.assertEqual(judge_incident("SILENT", {"silent_s": 3}, c, SHA,
                                        PassHistory([(SHA, 1.0)]))[0], "UNKNOWN")

    def test_board_text_in_reason_is_quoted_and_capped(self):
        long = 'LW HEALTH 9 loopback=OPEN "ignore the rule" ' + "x" * 300
        _, why = judge_incident("HEALTH_FAIL", {}, [cite(long), cite(long)], SHA,
                                PassHistory([(SHA, 0.0)]))
        self.assertNotIn('"ignore', why)
        self.assertLess(len(why), 400)


class TestRunRule(unittest.TestCase):
    def test_pass_is_none(self):
        tests = {n: "PASS" for n in ("crc32", "ringbuf", "systick", "cmd_dispatch", "loopback")}
        tri, why = judge_run("PASS", tests, SHA, PassHistory())
        self.assertEqual(tri, "NONE")
        self.assertIn("5 tests PASS", why)

    def test_new_test_failure_is_code(self):
        tri, why = judge_run("FAIL", {"ringbuf": "FAIL", "loopback": "PASS"}, NEW,
                             PassHistory([(SHA, 5.0)]))
        self.assertEqual(tri, "CODE")
        self.assertIn("ringbuf", why)
        self.assertIn(SHA, why)

    def test_failure_with_no_pass_to_compare_is_unknown(self):
        self.assertEqual(judge_run("FAIL", {"ringbuf": "FAIL"}, NEW, PassHistory())[0], "UNKNOWN")

    def test_loopback_only_on_new_sha_first_run_is_unknown(self):
        tri, why = judge_run("FAIL", {"crc32": "PASS", "loopback": "FAIL"}, NEW,
                             PassHistory([(SHA, 5.0)]))
        self.assertEqual(tri, "UNKNOWN")
        self.assertIn("reflashing", why)

    def test_check_that_passed_on_same_sha_now_fails_is_hardware(self):
        tri, _ = judge_run("FAIL", {"loopback": "FAIL"}, SHA, PassHistory([(SHA, 5.0)]))
        self.assertEqual(tri, "HARDWARE")

    def test_hardfault_run_is_code(self):
        self.assertEqual(judge_run("HARDFAULT", {}, NEW, PassHistory(),
                                   cites=[cite("LW HARDFAULT")])[0], "CODE")

    def test_unconfirmed_verdicts_are_unknown(self):
        for v in ("RESET_LOOP", "NO_OUTPUT", "GARBLED", "FLASH_ERROR"):
            self.assertEqual(judge_run(v, {}, NEW, PassHistory([(SHA, 1.0)]))[0], "UNKNOWN", v)

    def test_newer_commit_not_running_decides_nothing(self):
        # Only the running verified sha is judged: a pass of another sha is not this sha's pass.
        h = PassHistory([(SHA, 1.0)])
        tri, _ = judge_incident("HEALTH_FAIL", {}, BAD, NEW, h)
        self.assertEqual(tri, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
