"""Lane A: the incident state machine on synthetic, timestamped line sequences.

Lines are written by hand from the README "Serial output" protocol; shas are
invented.
"""

import unittest

from bench import parser
from bench.incidents import Machine

SHA = "abcdef1"
OTHER = "1234567"


class Feed:
    def __init__(self, **kw):
        self.m = Machine("nucleo-1", **kw)
        self.out = []
        self.n = 0
        self.add(self.m.link_up(0.0))

    def add(self, outs):
        self.out += outs
        return outs

    def line(self, t, text):
        outs = []
        for rec in parser.parse_line(text)["records"]:
            self.n += 1
            cite = {"event_id": self.n, "raw_ts": "t%.1f" % t, "line": rec["line"]}
            outs += self.m.record(t, rec, cite)
        return self.add(outs)

    def tick(self, t):
        return self.add(self.m.tick(t))

    def health(self, t, n, state, alive=True):
        outs = []
        if alive:
            outs += self.line(t, "LW ALIVE %d" % n)
        outs += self.line(t + 0.01, "LW HEALTH %d loopback=%s" % (n, state))
        outs += self.tick(t + 0.02)
        return outs

    def types(self, typ):
        return [o for o in self.out if o["type"] == typ]

    def boot_run(self, t, sha=SHA, reset="PIN", result="PASS"):
        self.line(t, "LW BOOT fw=%s reset=%s csr=0x04000000" % (sha, reset))
        for i, name in enumerate(("crc32", "ringbuf", "systick", "cmd_dispatch", "loopback")):
            self.line(t + 0.01 * (i + 1), "LW TEST %s PASS" % name)
        self.line(t + 0.1, "LW RESULT %s" % result)


def pattern(f, t0, n0, states, step=1.0):
    t, n = t0, n0
    for s in states:
        f.health(t, n, s)
        t += step
        n += 1
    return t, n


class TestHealth(unittest.TestCase):
    def test_one_bad_line_opens_nothing(self):
        f = Feed()
        pattern(f, 1, 1, ["OK", "OPEN", "OK", "STUCK", "OK", "OK"])
        self.assertEqual(f.types("open"), [])

    def test_open_on_two_bad_in_a_row(self):
        f = Feed()
        pattern(f, 1, 1, ["OK", "OK", "OPEN"])
        self.assertEqual(f.types("open"), [])
        opened = f.health(4, 4, "OPEN")
        self.assertEqual([o["type"] for o in opened], ["open"])
        inc = opened[0]["inc"]
        self.assertEqual(inc.kind, "HEALTH_FAIL")
        self.assertAlmostEqual(inc.t_first_bad, 3.01)
        self.assertAlmostEqual(inc.opened, 4.01)
        self.assertEqual(opened[0]["cite"]["line"], "LW HEALTH 3 loopback=OPEN")
        self.assertLess(inc.opened - inc.t_first_bad, 2.0)

    def test_stuck_counts_as_bad(self):
        f = Feed()
        pattern(f, 1, 1, ["STUCK", "OPEN"])
        self.assertEqual(len(f.types("open")), 1)

    def test_resolve_on_three_ok_with_duration(self):
        f = Feed()
        pattern(f, 1, 1, ["OPEN", "OPEN", "OPEN", "OK", "OK"])
        self.assertEqual(f.types("resolve"), [])
        f.health(6, 6, "OK")
        res = f.types("resolve")
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0]["duration"], 6.01 - 2.01, places=3)
        self.assertEqual(res[0]["inc"].state, "RESOLVED")
        # the resolve alert is held for the flap window, then sent once
        f.tick(60)
        self.assertEqual(f.types("resolve_alert"), [])
        f.tick(66.02)
        f.tick(70)
        self.assertEqual(len(f.types("resolve_alert")), 1)

    def test_ok_streak_broken_does_not_resolve(self):
        f = Feed()
        pattern(f, 1, 1, ["OPEN", "OPEN", "OK", "OK", "OPEN", "OK", "OK"])
        self.assertEqual(f.types("resolve"), [])
        self.assertEqual(len(f.types("open")), 1)


class TestFlap(unittest.TestCase):
    def test_reopen_within_60s_is_a_flap(self):
        f = Feed()
        t, n = pattern(f, 1, 1, ["OPEN", "OPEN", "OK", "OK", "OK"])
        t, n = pattern(f, t + 30, n, ["OPEN", "OPEN"])
        self.assertEqual(len(f.types("open")), 1)
        flaps = f.types("flap")
        self.assertEqual(len(flaps), 1)
        self.assertIs(flaps[0]["inc"], f.types("open")[0]["inc"])
        self.assertEqual(flaps[0]["inc"].state, "FLAPPING")
        self.assertEqual(flaps[0]["inc"].flaps, 1)
        self.assertTrue(flaps[0]["alert"])

    def test_reopen_after_60s_is_a_new_incident(self):
        f = Feed()
        t, n = pattern(f, 1, 1, ["OPEN", "OPEN", "OK", "OK", "OK"])
        pattern(f, t + 61, n, ["OPEN", "OPEN"])
        self.assertEqual(len(f.types("open")), 2)
        self.assertEqual(f.types("flap"), [])

    def test_five_pulls_1s_apart(self):
        # Jumper out ~1 s, in ~1 s, five times: the health samples alternate.
        f = Feed()
        t, n = pattern(f, 1, 1, ["OK", "OK"])
        t, n = pattern(f, t, n, ["OPEN", "OPEN", "OK", "OPEN", "OK", "OPEN", "OK", "OPEN",
                                 "OK", "OPEN"])
        t, n = pattern(f, t, n, ["OK"] * 5)
        self.assertEqual(len(f.types("open")), 1)
        self.assertEqual(len(f.types("resolve")), 1)
        self.assertEqual(f.types("flap"), [])

    def test_five_pulls_5s_apart(self):
        f = Feed()
        t, n = pattern(f, 1, 1, ["OK", "OK"])
        for _ in range(5):
            t, n = pattern(f, t, n, ["OPEN", "OPEN", "OK", "OK", "OK"])
        t, n = pattern(f, t, n, ["OK"] * 5)
        opens, flaps = f.types("open"), f.types("flap")
        self.assertEqual(len(opens), 1)
        inc = opens[0]["inc"]
        self.assertEqual(inc.flaps, 4)
        self.assertEqual(len(flaps), 4)
        self.assertEqual([o["alert"] for o in flaps], [True, False, False, False])
        self.assertEqual(inc.state, "RESOLVED")
        for s in range(int(t), int(t) + 120):
            f.tick(s)
        self.assertEqual(f.types("flap_alert"), [])  # resolved before the window passed
        self.assertEqual(len(f.types("resolve_alert")), 1)

    def test_held_flap_goes_out_after_60s_if_still_open(self):
        f = Feed()
        t, n = pattern(f, 1, 1, ["OPEN", "OPEN", "OK", "OK", "OK"])
        t, n = pattern(f, t, n, ["OPEN", "OPEN", "OK", "OK", "OK"])   # flap 1 (alert)
        t, n = pattern(f, t, n, ["OPEN", "OPEN"])                     # flap 2 (held)
        first_alert = f.types("flap")[0]["t"]
        t, n = pattern(f, t, n, ["OPEN"] * 60)
        held = f.types("flap_alert")
        self.assertEqual(len(held), 1)
        self.assertGreaterEqual(held[0]["t"] - first_alert, 60)
        self.assertEqual(held[0]["inc"].flaps, 2)


class TestSilentAndLink(unittest.TestCase):
    def test_silent_after_3s(self):
        f = Feed()
        f.health(1, 1, "OK")
        f.tick(3.9)
        self.assertEqual(f.types("open"), [])
        f.tick(4.02)
        opens = f.types("open")
        self.assertEqual([o["inc"].kind for o in opens], ["SILENT"])
        self.assertAlmostEqual(opens[0]["inc"].t_first_bad, 1.01)
        f.tick(10)
        self.assertEqual(len(f.types("open")), 1)
        f.line(11, "LW ALIVE 2")
        self.assertEqual(f.types("resolve")[0]["inc"].kind, "SILENT")

    def test_non_lw_text_is_still_silence(self):
        f = Feed()
        f.health(1, 1, "OK")
        f.line(2, "free text from the board")
        f.line(3, "\x00\xff garbage")
        f.tick(4.1)
        self.assertEqual([o["inc"].kind for o in f.types("open")], ["SILENT"])

    def test_no_silent_while_link_down(self):
        f = Feed()
        f.health(1, 1, "OK")
        f.add(f.m.link_down(1.5))
        f.tick(8)
        self.assertEqual(f.types("open"), [])

    def test_link_down_opens_after_10s_and_resolves_when_back(self):
        f = Feed()
        f.health(1, 1, "OK")
        f.add(f.m.link_down(2))
        f.tick(11.9)
        self.assertEqual(f.types("open"), [])
        f.tick(12)
        opens = f.types("open")
        self.assertEqual([o["inc"].kind for o in opens], ["LINK_DOWN"])
        self.assertEqual(opens[0]["inc"].t_first_bad, 2)
        f.tick(15)
        self.assertEqual(len(f.types("open")), 1)
        f.add(f.m.link_up(22))
        res = f.types("resolve")
        self.assertEqual(res[0]["inc"].kind, "LINK_DOWN")
        self.assertAlmostEqual(res[0]["duration"], 10)

    def test_5s_replug_counter_restart_is_logged_not_alerted(self):
        f = Feed()
        t, n = pattern(f, 1, 1, ["OK"] * 30)
        f.add(f.m.link_down(t))
        f.tick(t + 3)
        f.add(f.m.link_up(t + 5))
        # BOOT line missed after the replug: the counter restarts at 1.
        pattern(f, t + 5.5, 1, ["OK"] * 5)
        self.assertEqual(f.types("open"), [])
        logs = [o for o in f.types("log") if o["what"] == "REPLUG"]
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["how"], "counter")

    def test_replug_boot_line_seen_is_not_a_reboot(self):
        f = Feed()
        t, n = pattern(f, 1, 1, ["OK"] * 5)
        f.add(f.m.link_down(t))
        f.add(f.m.link_up(t + 5))
        f.boot_run(t + 5.2)
        pattern(f, t + 6, 1, ["OK"] * 4)
        self.assertEqual(f.types("open"), [])

    def test_20s_pull_one_link_down_and_back_no_reboot(self):
        f = Feed()
        t, n = pattern(f, 1, 1, ["OK"] * 10)
        f.add(f.m.link_down(t))
        for s in range(1, 21):
            f.tick(t + s)
        f.add(f.m.link_up(t + 20))
        pattern(f, t + 20.5, 1, ["OK"] * 5)
        self.assertEqual([o["inc"].kind for o in f.types("open")], ["LINK_DOWN"])
        self.assertEqual(len(f.types("resolve")), 1)

    def test_counter_restart_outside_replug_window_is_a_reboot(self):
        f = Feed()
        t, n = pattern(f, 1, 1, ["OK"] * 10)
        f.add(f.m.link_down(t))
        f.add(f.m.link_up(t + 5))
        pattern(f, t + 16, 1, ["OK"])  # 11 s after the port came back
        self.assertEqual([o["inc"].kind for o in f.types("open")], ["REBOOT"])


class TestRebootAndFlash(unittest.TestCase):
    def test_reset_button_is_one_reboot(self):
        f = Feed()
        t, n = pattern(f, 1, 1, ["OK"] * 5)
        f.boot_run(t)
        pattern(f, t + 1, 1, ["OK"] * 5)
        opens = f.types("open")
        self.assertEqual([o["inc"].kind for o in opens], ["REBOOT"])
        self.assertEqual(opens[0]["inc"].facts["reset"], ["PIN"])
        res = f.types("resolve")
        self.assertEqual(len(res), 1)
        for s in range(int(t), int(t) + 120):
            f.tick(s)
        self.assertEqual(f.types("resolve_alert"), [])  # no resolve alert for a reboot

    def test_reset_loop_boots_are_one_incident(self):
        f = Feed()
        f.health(1, 1, "OK")
        for i in range(6):
            f.line(2 + 1.5 * i, "LW BOOT fw=%s reset=IWDG,PIN csr=0x24000000" % SHA)
            f.tick(2.5 + 1.5 * i)
        self.assertEqual(len(f.types("open")), 1)
        self.assertEqual(f.types("open")[0]["inc"].repeats, 5)

    def test_bench_flash_boot_is_not_a_reboot_and_stale_bytes_are_dropped(self):
        f = Feed()
        f.health(1, 1, "OK")
        f.add(f.m.expect_flash(2, SHA, window=15))
        # stale output of the previous build, then its boot: all discarded
        f.line(2.1, "LW HEALTH 2 loopback=OPEN")
        f.line(2.2, "LW HEALTH 3 loopback=OPEN")
        f.line(2.3, "LW HARDFAULT pc=0x1 lr=0x2")
        f.line(2.4, "LW BOOT fw=%s reset=PIN csr=0x04000000" % OTHER)
        f.tick(6)  # no SILENT while waiting for the boot
        self.assertEqual(f.types("open"), [])
        f.boot_run(7)
        starts = f.types("run_start")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]["discarded"], 4)
        self.assertEqual(f.m.verified_sha, SHA)
        ends = f.types("run_end")
        self.assertEqual(ends[0]["run"]["verdict"], "PASS")
        self.assertEqual(ends[0]["run"]["tests"]["loopback"], "PASS")
        self.assertEqual(f.types("open"), [])

    def test_flash_error_when_no_boot_in_window(self):
        f = Feed()
        f.add(f.m.expect_flash(1, SHA, window=15))
        f.tick(15.9)
        self.assertEqual(f.types("run_end"), [])
        f.tick(16)
        self.assertEqual(f.types("run_end")[0]["run"]["verdict"], "FLASH_ERROR")
        self.assertIsNone(f.m.verified_sha)

    def test_unverified_result_pass_is_no_run(self):
        f = Feed()
        f.boot_run(1)
        self.assertEqual(f.types("run_end"), [])
        self.assertIsNone(f.m.verified_sha)

    def test_boot_of_another_sha_unverifies(self):
        f = Feed()
        f.add(f.m.expect_flash(0.5, SHA))
        f.boot_run(1)
        self.assertEqual(f.m.verified_sha, SHA)
        f.boot_run(10, sha=OTHER)
        self.assertIsNone(f.m.verified_sha)
        self.assertTrue(any(o["what"] == "UNVERIFIED" for o in f.types("log")))

    def test_result_fail_opens_run_fail(self):
        f = Feed()
        f.add(f.m.expect_flash(0.5, SHA))
        f.line(1, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        f.line(1.1, "LW TEST ringbuf FAIL got=0x1 want=0x2")
        f.line(1.2, "LW RESULT FAIL")
        opens = f.types("open")
        self.assertEqual([o["inc"].kind for o in opens], ["RUN_FAIL"])
        self.assertEqual(opens[0]["inc"].run["tests"], {"ringbuf": "FAIL"})

    def test_second_boot_in_run_is_reset_loop(self):
        f = Feed()
        f.add(f.m.expect_flash(0.5, SHA))
        f.line(1, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        f.line(2.5, "LW BOOT fw=%s reset=IWDG,PIN csr=0x24000000" % SHA)
        f.line(4, "LW BOOT fw=%s reset=IWDG,PIN csr=0x24000000" % SHA)
        self.assertEqual(f.types("run_end")[0]["run"]["verdict"], "RESET_LOOP")
        self.assertEqual([o["inc"].kind for o in f.types("open")], ["RUN_FAIL"])


class TestHardFault(unittest.TestCase):
    LINE = "LW HARDFAULT pc=0x08000100 lr=0x08000201 psr=0x01000000 cfsr=0x1 hfsr=0x2 " \
           "bfar=0x3 mmfar=0x4 sp=0x20001000"

    def test_repeating_hardfault_is_one_incident(self):
        f = Feed()
        f.add(f.m.expect_flash(0.5, SHA))
        f.line(1, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        f.line(1.1, "LW TEST crc32 PASS")
        for i in range(10):
            f.line(1.2 + i, self.LINE)
            f.tick(1.5 + i)
        # repeats glued with no line break are the same incident too
        f.line(12, self.LINE + self.LINE + "LW HARDFAULT")
        opens = f.types("open")
        self.assertEqual([o["inc"].kind for o in opens], ["HARDFAULT"])
        self.assertEqual(opens[0]["inc"].repeats, 12)
        self.assertEqual(f.types("run_end")[0]["run"]["verdict"], "HARDFAULT")
        self.assertEqual(opens[0]["inc"].facts["fault"]["cfsr"], "0x1")
        f.line(20, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        self.assertIn("HARDFAULT", [o["inc"].kind for o in f.types("resolve")])

    def test_hardfault_without_fields_is_a_fault(self):
        f = Feed()
        f.line(1, "LW HARDFAULT")
        self.assertEqual([o["inc"].kind for o in f.types("open")], ["HARDFAULT"])
        self.assertTrue(f.types("open")[0]["inc"].facts["partial"])


class TestCanSend(unittest.TestCase):
    def test_only_after_result_and_one_alive(self):
        f = Feed()
        self.assertFalse(f.m.can_send())
        f.boot_run(1)
        self.assertFalse(f.m.can_send())
        f.line(2, "LW ALIVE 1")
        self.assertTrue(f.m.can_send())
        f.line(3, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        self.assertFalse(f.m.can_send())
        f.add(f.m.link_down(4))
        self.assertFalse(f.m.can_send())


if __name__ == "__main__":
    unittest.main()
