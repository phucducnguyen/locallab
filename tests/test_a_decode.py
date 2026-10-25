"""Lane A: HardFault decode (bench.decode).

Register values are synthetic, chosen per architectural bit (ARMv7-M CFSR /
HFSR), never taken from a recording or the firmware's documentation. The
toolchain test builds an ELF from a git archive of ../fw-public main without
reading its sources and picks an address from arm-none-eabi-nm; it is skipped
when the toolchain is absent.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from bench import alerts, decode, parser

HERE = os.path.dirname(os.path.abspath(__file__))
FW = os.environ.get("LW_FW_REPO") or os.path.join(os.path.dirname(os.path.dirname(HERE)),
                                                  "fw-public")

ALL_BITS = ([("MMFSR", b, n) for b, n, _ in decode.MMFSR_BITS] +
            [("BFSR", b, n) for b, n, _ in decode.BFSR_BITS] +
            [("UFSR", b, n) for b, n, _ in decode.UFSR_BITS])


def fields(**kw):
    return {k: ("0x%08x" % v if isinstance(v, int) else v) for k, v in kw.items()}


class DecodeBits(unittest.TestCase):
    def test_every_cfsr_bit_by_name(self):
        for group, bit, name in ALL_BITS:
            # Each bit with a partner bit in another sub-register, so no value is a lone bit.
            partner = 16 if group != "UFSR" else 8
            pname = "UNDEFINSTR" if partner == 16 else "IBUSERR"
            v = (1 << bit) | (1 << partner)
            d = decode.decode_fault(fields(cfsr=v, hfsr=1 << 30))
            with self.subTest(name=name):
                self.assertIn(name, [x["name"] for x in d["cfsr"][group]])
                self.assertEqual(sorted(d["names"]), sorted(["FORCED", name, pname]))
                self.assertEqual(d["cfsr"]["reserved"], [])

    def test_every_hfsr_bit_by_name(self):
        for bit, name, _ in decode.HFSR_BITS:
            d = decode.decode_fault(fields(hfsr=(1 << bit), cfsr=0))
            with self.subTest(name=name):
                self.assertEqual([x["name"] for x in d["hfsr"]["bits"]], [name])
                self.assertEqual(d["hfsr"]["reserved"], [])

    def test_reserved_bits_reported_not_named(self):
        d = decode.decode_fault(fields(cfsr=(1 << 2) | (1 << 14) | (1 << 20) | (1 << 9),
                                       hfsr=(1 << 5) | (1 << 30)))
        self.assertEqual(d["cfsr"]["reserved"], [2, 14, 20])
        self.assertEqual([x["name"] for x in d["cfsr"]["BFSR"]], ["PRECISERR"])
        self.assertEqual(d["hfsr"]["reserved"], [5])

    def test_summary_one_line(self):
        d = decode.decode_fault(fields(cfsr=(1 << 18) | (1 << 25), hfsr=1 << 30,
                                       pc=0x08001234, lr=0x08000abd, psr=0x01000000))
        s = d["summary"]
        self.assertNotIn("\n", s)
        self.assertIn("HFSR FORCED", s)
        self.assertIn("UFSR INVPC+DIVBYZERO", s)
        self.assertIn("pc=0x08001234", s)
        self.assertTrue(d["thumb"])
        d = decode.decode_fault(fields(cfsr=1 << 16, psr=0x00000000))
        self.assertIs(d["thumb"], False)
        self.assertIn("EPSR.T=0", d["summary"])


class DecodeGating(unittest.TestCase):
    BF, MM = 0x2001fff0, 0x20000444

    def test_bfar_only_with_bfarvalid(self):
        d = decode.decode_fault(fields(cfsr=(1 << 9), bfar=self.BF, mmfar=self.MM))
        self.assertIsNone(d["bfar"])
        self.assertIn("BFARVALID clear", d["bfar_note"])
        self.assertNotIn("%08x" % self.BF, d["summary"])
        d = decode.decode_fault(fields(cfsr=(1 << 9) | (1 << 15), bfar=self.BF, mmfar=self.MM))
        self.assertEqual(d["bfar"], "0x%08x" % self.BF)
        self.assertIn("BFAR=0x%08x" % self.BF, d["summary"])
        self.assertIsNone(d["mmfar"])
        self.assertNotIn("BFARVALID", d["summary"])  # a flag, not a fault

    def test_mmfar_only_with_mmarvalid(self):
        d = decode.decode_fault(fields(cfsr=(1 << 1), bfar=self.BF, mmfar=self.MM))
        self.assertIsNone(d["mmfar"])
        self.assertIn("MMARVALID clear", d["mmfar_note"])
        d = decode.decode_fault(fields(cfsr=(1 << 1) | (1 << 7), bfar=self.BF, mmfar=self.MM))
        self.assertEqual(d["mmfar"], "0x%08x" % self.MM)
        self.assertIsNone(d["bfar"])
        self.assertIn("MMFAR=0x%08x" % self.MM, d["summary"])

    def test_valid_flag_but_register_missing(self):
        d = decode.decode_fault(fields(cfsr=(1 << 15) | (1 << 7)))
        self.assertIsNone(d["bfar"])
        self.assertIsNone(d["mmfar"])
        self.assertIn("missing", d["bfar_note"])

    def test_missing_and_bad_fields(self):
        d = decode.decode_fault({})
        self.assertEqual(d["missing"], list(decode.REG_FIELDS))
        self.assertIsNone(d["cfsr"])
        self.assertIn("no CFSR/HFSR", d["summary"])
        d = decode.decode_fault({"cfsr": "0x..", "hfsr": "zz", "pc": "0x8000100"})
        self.assertEqual(sorted(d["invalid"]), ["cfsr", "hfsr"])
        self.assertEqual(d["regs"]["pc"], "0x08000100")
        self.assertIsNone(decode.parse_reg("0x123456789"))

    def test_from_parser_record(self):
        rec = parser.parse_line("LW HARDFAULT pc=0x080004a2 lr=0x08000311 psr=0x21000000 "
                                "cfsr=0x00008200 hfsr=0x40000000 bfar=0x40023c99 "
                                "mmfar=0xe000ed34 sp=0x2001ffa0")["records"][0]
        d = decode.decode_fault(rec)
        self.assertEqual(sorted(d["names"]), ["BFARVALID", "FORCED", "PRECISERR"])
        self.assertEqual(d["bfar"], "0x40023c99")
        self.assertIsNone(d["mmfar"])
        partial = parser.parse_line("LW HARDFAULT")["records"][0]
        self.assertEqual(decode.decode_fault(partial)["missing"], list(decode.REG_FIELDS))


class FakeRun:
    def __init__(self, out, rc=0):
        self.out, self.rc, self.cmd = out, rc, None

    def __call__(self, cmd, **kw):
        self.cmd = cmd
        return subprocess.CompletedProcess(cmd, self.rc, self.out.encode(), b"")


class Addr2lineParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.elf = os.path.join(self.tmp, "abc1234.elf")
        open(self.elf, "wb").close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_pc_and_lr_minus_one(self):
        run = FakeRun("widget_poll\n/build/src/widget.c:42\nwidget_main\n"
                      "/build/src/widget.c:97 (discriminator 2)\n")
        a = decode.addr2line(self.elf, "0x08000200", "0x08000151", tool="a2l", runner=run,
                             root="/build")
        self.assertEqual(run.cmd, ["a2l", "-f", "-e", self.elf, "0x8000200", "0x8000150"])
        self.assertEqual(a["pc"]["file_line"], "src/widget.c:42")
        self.assertEqual(a["lr"]["function"], "widget_main")
        self.assertEqual(a["lr"]["file_line"], "src/widget.c:97")
        self.assertEqual(a["lr"]["addr"], "0x08000150")
        self.assertEqual((a["file_line"], a["function"]), ("src/widget.c:42", "widget_poll"))

    def test_unknown_pc_falls_back_to_lr(self):
        run = FakeRun("??\n??:0\nwidget_main\nsrc/widget.c:97\n")
        a = decode.addr2line(self.elf, 0, 0x08000151, tool="a2l", runner=run)
        self.assertIsNone(a["pc"]["function"])
        self.assertIsNone(a["pc"]["file_line"])
        self.assertEqual(a["file_line"], "src/widget.c:97")

    def test_exc_return_lr_not_looked_up(self):
        run = FakeRun("widget_poll\nsrc/widget.c:42\n")
        a = decode.addr2line(self.elf, 0x08000200, 0xFFFFFFF9, tool="a2l", runner=run)
        self.assertEqual(run.cmd[-1], "0x8000200")
        self.assertIn("EXC_RETURN", a["lr"]["note"])

    def test_missing_pieces_never_raise(self):
        a = decode.addr2line(None, "0x1", "0x3")
        self.assertEqual(a["error"], "no ELF for this sha")
        a = decode.addr2line(self.elf, None, None, tool="a2l", runner=FakeRun(""))
        self.assertIsNone(a["file_line"])
        a = decode.addr2line(self.elf, 0x100, 0x201, tool=os.path.join(self.tmp, "nope"))
        self.assertIn("failed", a["error"])

    def test_elf_path(self):
        self.assertEqual(decode.elf_path(self.tmp, "abc1234"), self.elf)
        self.assertEqual(decode.elf_path(self.tmp, "abc1234deadbeef"), self.elf)
        self.assertIsNone(decode.elf_path(self.tmp, "fff0000"))

    def test_evidence_and_first_alert(self):
        run = FakeRun("widget_poll\nsrc/widget.c:42\nwidget_main\nsrc/widget.c:97\n")
        ev = decode.fault_evidence(fields(pc=0x08000200, lr=0x08000151, cfsr=1 << 16,
                                          hfsr=1 << 30), self.elf, tool="a2l", runner=run)
        self.assertEqual(ev["file_line"], "src/widget.c:42")
        inc = {"id": 7, "board": "nucleo-1", "kind": "HARDFAULT", "triage": "CODE",
               "route": "Firmware team", "reason": "r", "evidence": json.dumps(ev)}
        text = alerts.alert_text("open", inc, {"raw_ts": "12:00:00.000", "line": "LW HARDFAULT"})
        self.assertIn("Fault: HFSR FORCED; UFSR UNDEFINSTR", text)
        self.assertIn("in widget_poll()", text)
        self.assertIn("File: src/widget.c:42", text)
        lines = text.splitlines()
        self.assertLess(lines.index([x for x in lines if x.startswith("Fault:")][0]),
                        lines.index("File: src/widget.c:42"))
        inc["evidence"] = "{}"
        self.assertNotIn("Fault:", alerts.alert_text("open", inc))


TOOLS = ("arm-none-eabi-gcc", "arm-none-eabi-nm", "arm-none-eabi-addr2line", "make", "git")


@unittest.skipUnless(all(shutil.which(t) for t in TOOLS) and os.path.isdir(os.path.join(FW, ".git")),
                     "no arm-none-eabi toolchain or ../fw-public is not a git repo here")
class Addr2lineRealElf(unittest.TestCase):
    """Build main from a git archive (sources unread), look up an nm address."""

    def test_nm_symbol_resolves(self):
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "src")
            os.mkdir(src)
            arch = subprocess.run(["git", "-C", FW, "archive", "--format=tar", "main"],
                                  stdout=subprocess.PIPE, check=True).stdout
            subprocess.run(["tar", "-x", "-C", src], input=arch, check=True)
            subprocess.run(["make", "-C", src, "FW_VERSION=0a1b2c3"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            elf = os.path.join(tmp, "0a1b2c3.elf")
            shutil.copy(os.path.join(src, "build", "fw.elf"), elf)
            nm = subprocess.run(["arm-none-eabi-nm", "-S", "--defined-only", elf],
                                stdout=subprocess.PIPE, check=True).stdout.decode()
            sym = None
            for ln in nm.splitlines():
                p = ln.split()
                if len(p) == 4 and p[2] in "Tt" and int(p[1], 16) >= 8:
                    sym = (int(p[0], 16) & ~1, int(p[1], 16), p[3])
                    if p[3] == "main":
                        break
            self.assertIsNotNone(sym)
            addr, size, name = sym
            a = decode.addr2line(elf, addr + 2, (addr + size - 2) | 1, root=src)
            self.assertIsNone(a["error"])
            self.assertEqual(a["pc"]["function"], name)
            self.assertEqual(a["lr"]["function"], name)
            self.assertRegex(a["file_line"] or "", r"\.[cSs]:\d+$")
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
