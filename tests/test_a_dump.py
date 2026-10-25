"""Lane A: DumpCollector pairs a LW DUMP burst with the next HARDFAULT. Invented values."""

import unittest

from bench import parser
from bench.dump import DumpCollector

REG = ("LW DUMP reg r0=0x00000005 r1=0x00000006 r2=0x20000100 r3=0x00000000 "
       "r12=0x0000ffff lr=0xfffffff9 pc=0x080023a4 psr=0x21000003")
S1 = "LW DUMP stack 0x2001ff80 0x0000000a 0x0000000b 0x0000000c 0x0000000d"
S2 = "LW DUMP stack 0x2001ff90 0x0800aaaa 0x0800bbbb 0x00000000 0x01000000"
CAUSE = "LW DUMP cause FORCED UNDEFINSTR"
END = "LW DUMP end"
HF = ("LW HARDFAULT pc=0x080023a4 lr=0xfffffff9 psr=0x21000003 cfsr=0x00010000 "
      "hfsr=0x40000000 bfar=0x00000000 mmfar=0x00000000 sp=0x2001ff80")
BOOT = "LW BOOT fw=c0ffee1 reset=PIN csr=0x04000000"


def recs(line):
    return parser.parse_line(line)["records"]


class DumpCollectorTest(unittest.TestCase):
    def setUp(self):
        self.c = DumpCollector()

    def feed(self, t, line):
        out = []
        for r in recs(line):
            out.append(self.c.feed(t, r, cite={"line": r["line"]}))
        return out[-1] if out else None

    def burst(self, t, lines=(REG, S1, S2, CAUSE, END)):
        for i, line in enumerate(lines):
            self.assertIsNone(self.feed(t + i * 0.01, line))

    def test_full_burst_then_hardfault(self):
        self.burst(10.0)
        d = self.feed(10.2, HF)
        self.assertTrue(d["complete"])
        self.assertEqual(d["registers"]["lr"], "0xfffffff9")
        self.assertEqual(len(d["registers"]), 8)
        self.assertEqual(d["stack"], [("0x2001ff80", ["0x0000000a", "0x0000000b",
                                                      "0x0000000c", "0x0000000d"]),
                                      ("0x2001ff90", ["0x0800aaaa", "0x0800bbbb",
                                                      "0x00000000", "0x01000000"])])
        self.assertIsNone(d["stack_bad_sp"])
        self.assertEqual(d["cause_names"], ["FORCED", "UNDEFINSTR"])
        self.assertEqual(len(d["cites"]), 5)

    def test_hardfault_repeats_with_no_new_burst(self):
        self.burst(10.0)
        self.assertIsNotNone(self.feed(10.2, HF))
        for k in range(1, 6):
            self.assertIsNone(self.feed(10.2 + k, HF))

    def test_hardfault_with_no_burst(self):
        self.assertIsNone(self.feed(1.0, HF))

    def test_stack_bad_sp(self):
        self.burst(5.0, (REG, "LW DUMP stack bad sp=0x50000000", "LW DUMP cause FORCED STKERR",
                         END))
        d = self.feed(5.1, HF)
        self.assertTrue(d["complete"])
        self.assertEqual(d["stack"], [])
        self.assertEqual(d["stack_bad_sp"], "0x50000000")

    def test_cause_none_is_empty_list(self):
        self.burst(5.0, (REG, S1, "LW DUMP cause NONE", END))
        d = self.feed(5.1, HF)
        self.assertEqual(d["cause_names"], [])
        self.assertTrue(d["complete"])

    def test_glued_lines_inside_burst(self):
        self.feed(3.0, REG + S1)
        self.feed(3.01, S2 + CAUSE + END)
        d = self.feed(3.02, HF)
        self.assertTrue(d["complete"])
        self.assertEqual(len(d["stack"]), 2)
        # The whole burst and the HARDFAULT glued onto one line.
        c = DumpCollector()
        out = [c.feed(4.0, r) for r in recs(REG + S1 + CAUSE + END + HF)]
        self.assertEqual(out[:4], [None] * 4)
        self.assertTrue(out[4]["complete"])

    def test_missing_end(self):
        self.burst(7.0, (REG, S1, CAUSE))
        d = self.feed(7.1, HF)
        self.assertFalse(d["complete"])
        self.assertEqual(d["cause_names"], ["FORCED", "UNDEFINSTR"])
        self.assertEqual(len(d["stack"]), 1)

    def test_burst_seen_from_the_middle(self):
        self.burst(7.0, (S2, CAUSE, END))
        d = self.feed(7.1, HF)
        self.assertFalse(d["complete"])
        self.assertEqual(d["registers"], {})
        self.assertEqual(d["stack"][0][0], "0x2001ff90")

    def test_burst_timeout_discards(self):
        self.burst(20.0)
        self.assertIsNone(self.feed(23.5, HF))
        self.assertEqual(self.c.discarded, 1)

    def test_burst_within_timeout_kept(self):
        self.burst(20.0)
        self.assertIsNotNone(self.feed(22.9, HF))

    def test_timeout_measured_from_last_dump_line(self):
        self.feed(30.0, REG)
        self.feed(32.0, S1)
        self.feed(34.5, CAUSE)  # 2.5 s after the stack line: still the same burst
        self.feed(34.6, END)
        d = self.feed(37.0, HF)
        self.assertIsNotNone(d)
        self.assertTrue(d["complete"])

    def test_expire_without_records(self):
        self.burst(1.0)
        self.c.expire(10.0)
        self.assertIsNone(self.c.burst)
        self.assertIsNone(self.feed(10.0, HF))

    def test_second_burst_after_reset(self):
        self.burst(1.0)
        first = self.feed(1.1, HF)
        self.assertIsNone(self.feed(2.1, HF))
        self.feed(3.0, BOOT)
        self.burst(4.0, (REG.replace("0x080023a4", "0x08009990"),
                         "LW DUMP stack bad sp=0x10000000", "LW DUMP cause FORCED INVPC", END))
        second = self.feed(4.1, HF)
        self.assertEqual(first["registers"]["pc"], "0x080023a4")
        self.assertEqual(second["registers"]["pc"], "0x08009990")
        self.assertEqual(second["cause_names"], ["FORCED", "INVPC"])
        self.assertEqual(second["stack"], [])

    def test_boot_discards_pending_burst(self):
        self.burst(1.0)
        self.feed(1.1, BOOT)
        self.assertIsNone(self.feed(1.2, HF))
        self.assertEqual(self.c.discarded, 1)

    def test_new_reg_starts_new_burst(self):
        self.burst(1.0, (REG, S1))
        self.burst(1.5, (REG.replace("0x00000005", "0x00000077"), S2, CAUSE, END))
        d = self.feed(1.6, HF)
        self.assertEqual(d["registers"]["r0"], "0x00000077")
        self.assertEqual([a for a, _ in d["stack"]], ["0x2001ff90"])
        self.assertTrue(d["complete"])
        self.assertEqual(self.c.discarded, 1)

    def test_malformed_line_marks_incomplete(self):
        self.burst(1.0, (REG, "LW DUMP stack 0x2001ff80 0xZZ", S1, CAUSE, END))
        d = self.feed(1.1, HF)
        self.assertFalse(d["complete"])
        self.assertEqual(len(d["stack"]), 1)

    def test_more_than_eight_stack_lines(self):
        stack = ["LW DUMP stack 0x%08x 0x00000000 0x00000000 0x00000000 0x00000000"
                 % (0x2001ff00 + 16 * i) for i in range(9)]
        self.burst(1.0, [REG] + stack + [CAUSE, END])
        d = self.feed(1.2, HF)
        self.assertEqual(len(d["stack"]), 8)
        self.assertFalse(d["complete"])

    def test_other_records_do_not_disturb(self):
        self.burst(1.0, (REG, S1, CAUSE, END))
        self.assertIsNone(self.feed(1.05, "LW ALIVE 3"))
        self.assertIsNotNone(self.feed(1.1, HF))


if __name__ == "__main__":
    unittest.main()
