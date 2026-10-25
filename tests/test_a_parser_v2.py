"""Lane A: parser grammar v2 (HEALTH vdd/temp, extra fields, LW DUMP). Invented values."""

import unittest

from bench import parser
from bench.serial_owner import LineSplitter

REG = ("LW DUMP reg r0=0x0000002a r1=0x20001f00 r2=0x00000000 r3=0xdeadbeef "
       "r12=0x00000007 lr=0x08001235 pc=0x08004c1e psr=0x2000001f")


class HealthV2Test(unittest.TestCase):
    def one(self, line):
        recs = parser.parse_line(line)["records"]
        self.assertEqual(len(recs), 1, recs)
        return recs[0]

    def test_without_vdd_temp_is_unknown(self):
        r = self.one("LW HEALTH 4 loopback=OK")
        self.assertEqual((r["n"], r["loopback"]), (4, "OK"))
        self.assertIsNone(r["vdd"])
        self.assertIsNone(r["temp"])
        self.assertNotIn("extra", r)
        self.assertNotIn("malformed", r)

    def test_with_vdd_temp(self):
        r = self.one("LW HEALTH 9 loopback=STUCK vdd=3287 temp=41")
        self.assertEqual((r["loopback"], r["vdd"], r["temp"]), ("STUCK", 3287, 41))
        r = self.one("LW HEALTH 9 loopback=OK temp=27")
        self.assertEqual((r["vdd"], r["temp"]), (None, 27))
        r = self.one("LW HEALTH 9 loopback=OK vdd=3311")
        self.assertEqual((r["vdd"], r["temp"]), (3311, None))

    def test_devices_by_key(self):
        r = self.one("LW HEALTH 7 loopback=OK sensor=OPEN radio=STUCK estop=ON vdd=3290 temp=33")
        self.assertEqual((r["loopback"], r["sensor"], r["radio"], r["estop"], r["vdd"], r["temp"]),
                         ("OK", "OPEN", "STUCK", "ON", 3290, 33))
        self.assertNotIn("extra", r)
        # Order does not matter; the keys do.
        r = self.one("LW HEALTH 7 estop=OFF temp=-1 radio=OK loopback=OPEN sensor=OK")
        self.assertEqual((r["loopback"], r["sensor"], r["radio"], r["estop"], r["temp"]),
                         ("OPEN", "OK", "OK", "OFF", -1))

    def test_devices_absent_on_old_firmware(self):
        r = self.one("LW HEALTH 1 loopback=OK")
        self.assertEqual((r["sensor"], r["radio"], r["estop"]), (None, None, None))
        self.assertNotIn("invalid", r)

    def test_devices_bad_values(self):
        r = self.one("LW HEALTH 1 loopback=OK sensor=MAYBE radio=ON estop=OK")
        self.assertEqual((r["sensor"], r["radio"], r["estop"]), (None, None, None))
        self.assertEqual(r["invalid"], {"sensor": "MAYBE", "radio": "ON", "estop": "OK"})
        self.assertNotIn("malformed", r)

    def test_negative_temp(self):
        self.assertEqual(self.one("LW HEALTH 2 loopback=OK vdd=3302 temp=-12")["temp"], -12)

    def test_bad_values_unknown_not_zero(self):
        r = self.one("LW HEALTH 2 loopback=OK vdd=-5 temp=hot")
        self.assertIsNone(r["vdd"])
        self.assertIsNone(r["temp"])
        self.assertEqual(r["invalid"], {"vdd": "-5", "temp": "hot"})
        self.assertNotIn("malformed", r)

    def test_junk_trailing_fields_kept_in_extra(self):
        r = self.one("LW HEALTH 5 loopback=OPEN vdd=3299 fan=on2 junkword rssi=-3")
        self.assertEqual(r["extra"], {"fan": "on2", "rssi": "-3"})
        self.assertEqual(r["vdd"], 3299)
        self.assertEqual(r["loopback"], "OPEN")


class ExtraOnOtherKindsTest(unittest.TestCase):
    def one(self, line):
        return parser.parse_line(line)["records"][0]

    def test_boot_hardfault_alive_result_other(self):
        b = self.one("LW BOOT fw=a1b2c3d reset=PIN csr=0x04000000 bl=v7")
        self.assertEqual(b["extra"], {"bl": "v7"})
        h = self.one("LW HARDFAULT pc=0x08000101 lr=0x08000203 core=m7")
        self.assertEqual(h["extra"], {"core": "m7"})
        self.assertEqual(h["pc"], "0x08000101")
        self.assertEqual(self.one("LW ALIVE 3 up=12")["extra"], {"up": "12"})
        self.assertEqual(self.one("LW RESULT PASS took=88")["extra"], {"took": "88"})
        o = self.one("LW FROB 1 x=2")
        self.assertEqual((o["kind"], o["word"], o["extra"]), ("OTHER", "FROB", {"x": "2"}))

    def test_free_text_kinds_unchanged(self):
        t = self.one("LW TEST demo FAIL got=1 want=2")
        self.assertEqual(t["detail"], "got=1 want=2")
        self.assertNotIn("extra", t)
        self.assertNotIn("extra", self.one("LW BOOT fw=a1b2c3d reset=PIN csr=0x04000000"))


class DumpParseTest(unittest.TestCase):
    def one(self, line):
        recs = parser.parse_line(line)["records"]
        self.assertEqual(len(recs), 1, recs)
        self.assertEqual(recs[0]["kind"], "DUMP")
        return recs[0]

    def test_reg(self):
        r = self.one(REG)
        self.assertEqual(r["sub"], "reg")
        self.assertEqual(r["regs"]["r12"], "0x00000007")
        self.assertEqual(r["regs"]["pc"], "0x08004c1e")
        self.assertEqual(len(r["regs"]), 8)
        self.assertFalse(r["partial"])

    def test_reg_partial_and_bad(self):
        r = self.one("LW DUMP reg r0=0x00000001 pc=0x08000010")
        self.assertEqual(r["sub"], "reg")
        self.assertTrue(r["partial"])
        self.assertEqual(self.one("LW DUMP reg r0=0x1")["sub"], "malformed")
        self.assertEqual(self.one("LW DUMP reg")["sub"], "malformed")

    def test_stack(self):
        r = self.one("LW DUMP stack 0x2001ffe0 0x00000011 0x00000022 0x00000033 0x00000044")
        self.assertEqual(r["sub"], "stack")
        self.assertEqual(r["addr"], "0x2001ffe0")
        self.assertEqual(r["words"], ["0x00000011", "0x00000022", "0x00000033", "0x00000044"])

    def test_stack_bad_sp(self):
        r = self.one("LW DUMP stack bad sp=0x3fff0010")
        self.assertEqual((r["sub"], r["bad_sp"]), ("stack", "0x3fff0010"))
        self.assertEqual(self.one("LW DUMP stack bad sp=zz")["sub"], "malformed")

    def test_stack_malformed(self):
        for line in ("LW DUMP stack", "LW DUMP stack 0x2001ffe0",
                     "LW DUMP stack 0x2001ffe0 0x1 0x00000002",
                     "LW DUMP stack 0x2001ffe0 " + " ".join(["0x00000000"] * 5)):
            r = self.one(line)
            self.assertEqual(r["sub"], "malformed", line)
            self.assertTrue(r["malformed"])

    def test_cause(self):
        r = self.one("LW DUMP cause FORCED PRECISERR BFARVALID")
        self.assertEqual((r["sub"], r["names"]), ("cause", ["FORCED", "PRECISERR", "BFARVALID"]))
        self.assertEqual(self.one("LW DUMP cause NONE")["names"], [])
        u = self.one("LW DUMP cause FORCED WIBBLE")
        self.assertEqual((u["sub"], u["names"], u["unknown"]), ("cause", ["FORCED"], ["WIBBLE"]))
        self.assertEqual(self.one("LW DUMP cause")["sub"], "malformed")
        self.assertEqual(self.one("LW DUMP cause NONE FORCED")["sub"], "malformed")

    def test_end_and_unknown_sub(self):
        self.assertEqual(self.one("LW DUMP end")["sub"], "end")
        self.assertEqual(self.one("LW DUMP")["sub"], "malformed")
        self.assertEqual(self.one("LW DUMP regs r0=0x00000000")["sub"], "malformed")

    def test_never_raises(self):
        for line in (b"LW DUMP \xff\xfe", "LW DUMP stack bad", "LW DUMP reg ===",
                     "LW DUMP cause " + "X" * 500, "LW DUMP stack bad sp=0x00000000 more"):
            for r in parser.parse_line(line)["records"]:
                self.assertEqual(r["kind"], "DUMP")

    def test_glued_burst_splits(self):
        glued = (REG + "LW DUMP stack 0x2001ffe0 0x00000001 0x00000002 0x00000003 0x00000004"
                 "LW DUMP cause NONELW DUMP endLW HARDFAULT pc=0x08004c1e")
        recs = parser.parse_line(glued)["records"]
        self.assertEqual([(r["kind"], r.get("sub")) for r in recs],
                         [("DUMP", "reg"), ("DUMP", "stack"), ("DUMP", "cause"),
                          ("DUMP", "end"), ("HARDFAULT", None)])
        self.assertEqual(recs[1]["words"][3], "0x00000004")

    def test_splitter_hands_over_glued_dump(self):
        sp = LineSplitter()
        lines = sp.feed(b"LW DUMP cause NONELW DUMP en")
        self.assertEqual(lines, [b"LW DUMP cause NONE"])
        lines = sp.feed(b"d\n")
        self.assertEqual(lines, [b"LW DUMP end"])

    def test_counters_count_dump(self):
        c = parser.Counters()
        c.add(parser.parse_line("LW DUMP endLW DUMP end"))
        self.assertEqual(c.by_kind, {"DUMP": 2})


if __name__ == "__main__":
    unittest.main()
