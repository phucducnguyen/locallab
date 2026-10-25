"""Lane A: parser and line splitter (no pyserial, no board)."""

import random
import unittest

from bench import parser
from bench.serial_owner import LineSplitter, valid_command


class ParseLineTest(unittest.TestCase):
    def one(self, line):
        p = parser.parse_line(line)
        self.assertEqual(len(p["records"]), 1, p)
        return p["records"][0]

    def test_boot_reset_list(self):
        r = self.one(b"LW BOOT fw=abc1234 reset=POR,PIN,BOR csr=0x0e000000\r")
        self.assertEqual(r["kind"], "BOOT")
        self.assertEqual(r["fw"], "abc1234")
        self.assertEqual(r["reset"], ["POR", "PIN", "BOR"])
        self.assertEqual(r["csr"], "0x0e000000")
        self.assertNotIn("malformed", r)

    def test_test_fail_detail(self):
        r = self.one("LW TEST demo FAIL got=1 want=2")
        self.assertEqual((r["name"], r["status"], r["detail"]), ("demo", "FAIL", "got=1 want=2"))

    def test_result_alive_health_reply(self):
        self.assertEqual(self.one("LW RESULT PASS")["status"], "PASS")
        self.assertEqual(self.one("LW ALIVE 12")["n"], 12)
        h = self.one("LW HEALTH 3 loopback=OPEN")
        self.assertEqual((h["n"], h["loopback"]), (3, "OPEN"))
        rep = self.one("LW REPLY ping ok")
        self.assertEqual((rep["cmd"], rep["text"]), ("ping", "ok"))

    def test_hardfault_fields_optional(self):
        r = self.one("LW HARDFAULT pc=0x1 lr=0x2")
        self.assertEqual(r["kind"], "HARDFAULT")
        self.assertEqual((r["pc"], r["lr"]), ("0x1", "0x2"))
        self.assertTrue(r["partial"])
        bare = self.one("LW HARDFAULT")
        self.assertEqual(bare["kind"], "HARDFAULT")
        self.assertTrue(bare["partial"])

    def test_glued_records_split(self):
        p = parser.parse_line("LW HARDFAULTLW HARDFAULTLW HARDFAULT pc=0x1")
        self.assertEqual([r["kind"] for r in p["records"]], ["HARDFAULT"] * 3)
        self.assertEqual(p["records"][2]["pc"], "0x1")
        p = parser.parse_line("LW ALIVE 1LW HEALTH 1 loopback=OK")
        self.assertEqual([r["kind"] for r in p["records"]], ["ALIVE", "HEALTH"])

    def test_no_split_inside_words(self):
        p = parser.parse_line("LW TEST x FAIL too SLOW here")
        self.assertEqual(len(p["records"]), 1)
        self.assertEqual(p["records"][0]["detail"], "too SLOW here")

    def test_anchor(self):
        p = parser.parse_line("hello LW BOOT fw=1 reset=PIN")
        self.assertEqual(p["records"], [])
        self.assertTrue(p["nonlw"])
        self.assertFalse(parser.parse_line("\r")["nonlw"])
        self.assertTrue(parser.parse_line("LWX")["nonlw"])

    def test_unknown_and_malformed(self):
        r = self.one("LW FROB 1")
        self.assertEqual((r["kind"], r["word"]), ("OTHER", "FROB"))
        self.assertTrue(self.one("LW ")["malformed"] if parser.parse_line("LW ")["records"] else True)
        self.assertTrue(self.one("LW ALIVE x")["malformed"])
        self.assertTrue(self.one("LW HEALTH 1 loopback=MAYBE")["malformed"])
        self.assertTrue(self.one("LW BOOT")["malformed"])
        self.assertTrue(self.one("LW TEST")["malformed"])

    def test_bad_bytes_counted_separately(self):
        p = parser.parse_line(b"\xff\x00abc\x80")
        self.assertEqual(p["bad_bytes"], 3)
        self.assertTrue(p["nonlw"])
        p = parser.parse_line(b"LW ALIVE 5\xfe")
        self.assertEqual(p["bad_bytes"], 1)
        self.assertEqual(p["records"][0]["kind"], "ALIVE")

    def test_never_raises(self):
        rnd = random.Random(1)
        alphabet = b"LW BOOTESTHARDFAULT=,0x\r\t\x00\xff"
        for _ in range(3000):
            n = rnd.randrange(0, 80)
            raw = bytes(rnd.choice(alphabet) for _ in range(n))
            p = parser.parse_line(raw)
            self.assertIn("records", p)
            parser.parse_line(raw.decode("latin-1"))
        for weird in (None, 5, b"LW " * 1000):
            parser.parse_line(weird)


class SplitterTest(unittest.TestCase):
    def test_lines_and_partial(self):
        s = LineSplitter()
        self.assertEqual(s.feed(b"LW ALIVE 1\r\nLW HEA"), [b"LW ALIVE 1\r"])
        self.assertEqual(s.feed(b"LTH 1 loopback=OK\r\n"), [b"LW HEALTH 1 loopback=OK\r"])
        self.assertEqual(s.feed(b"junk"), [])
        self.assertEqual(s.flush(), [b"junk"])

    def test_glued_records_released_early(self):
        s = LineSplitter()
        out = s.feed(b"LW HARDFAULTLW HARDFAULTLW HARDF")
        # A record counts as complete once the next full "LW <KIND>" token
        # follows it; the last two stay buffered until more bytes arrive.
        self.assertEqual(out, [b"LW HARDFAULT"])
        self.assertEqual(s.buf, b"LW HARDFAULTLW HARDF")
        self.assertEqual(s.feed(b"AULT\r\n"), [b"LW HARDFAULTLW HARDFAULT\r"])

    def test_cap(self):
        s = LineSplitter(max_line=10)
        self.assertEqual(s.feed(b"x" * 11), [b"x" * 11])


class CommandTest(unittest.TestCase):
    def test_valid_command(self):
        self.assertTrue(valid_command("ping"))
        self.assertTrue(valid_command("x" * 32))
        self.assertFalse(valid_command("x" * 33))
        self.assertFalse(valid_command(""))
        self.assertFalse(valid_command("ping\nping"))
        self.assertFalse(valid_command(b"ping"))


if __name__ == "__main__":
    unittest.main()
