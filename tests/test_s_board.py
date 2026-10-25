"""Lane S: the BOARD panel, new HEALTH keys, the DUMP burst and stopwatch markers."""

import json
import os
import time
import unittest

from bench import status_view
from bench.status_data import health_bad, parse_health
from tests.test_s_status_view import Base, once, run_emit

NEW_OK = "LW HEALTH 7 loopback=OK sensor=OK radio=OK estop=OFF vdd=3300 temp=31"


def panel(frame):
    sec = frame.split("BOARD (", 1)[1].split("STOPWATCH", 1)[0]
    return {line[2:20].strip(): line for line in sec.splitlines()[1:] if line.strip()}


class ParseTest(unittest.TestCase):
    def test_parse_by_key(self):
        h = parse_health("12:00:00.000 " + NEW_OK)
        self.assertEqual(h, {"n": 7, "loopback": "OK", "sensor": "OK", "radio": "OK",
                             "estop": "OFF", "vdd": 3300, "temp": 31.0})
        self.assertEqual(health_bad(h), [])
        old = parse_health("LW HEALTH 3 loopback=OPEN")
        self.assertEqual((old["sensor"], old["radio"], old["estop"], old["vdd"]),
                         (None, None, None, None))
        odd = parse_health("LW HEALTH x radio=STUCK loopback=OK vdd=junk estop=MAYBE")
        self.assertEqual(health_bad(odd), [("radio", "STUCK")])
        self.assertIsNone(odd["vdd"])
        self.assertIsNone(parse_health("LW ALIVE 3"))

    def test_line_classes(self):
        c = status_view.classify
        self.assertEqual(c(NEW_OK), "ok")
        self.assertEqual(c("LW HEALTH 7 loopback=OK sensor=OPEN radio=OK estop=OFF"), "bad")
        self.assertEqual(c("LW HEALTH 7 loopback=OK sensor=OK radio=OK estop=ON"), "bad")
        self.assertEqual(c("LW HEALTH 7 loopback=OK estop=OFF vdd=2900"), "bad")
        self.assertEqual(c("LW DUMP reg r0=0x0"), "bad")
        self.assertEqual(c("LW DUMP stack bad sp=0x1"), "bad")
        self.assertEqual(c("LW DUMP end"), "bad")


class PanelTest(Base):
    def log(self, *lines):
        p = os.path.join(self.tmp.name, "serial.log")
        with open(p, "w") as fh:
            fh.write("".join("12:00:%02d.000 %s\n" % (i, l) for i, l in enumerate(lines)))
        return once("--db", self.db, "--log", p, "--no-color")

    def test_all_green(self):
        p = panel(self.log("LW HEALTH 6 loopback=OPEN", NEW_OK))
        for k in ("loopback (A0-A1)", "sensor (A2-A3)", "radio (A4-A5)", "estop (B1)", "vdd"):
            self.assertTrue(p[k].startswith("+ "), p[k])
        self.assertIn("OFF", p["estop (B1)"])
        self.assertIn("3300 mV", p["vdd"])
        self.assertTrue(p["temp"].startswith("  ") and "31 C" in p["temp"])

    def test_red_devices_from_latest_line(self):
        f = self.log(NEW_OK, "LW HEALTH 8 loopback=OK sensor=OPEN radio=STUCK estop=ON vdd=2900")
        p = panel(f)
        self.assertTrue(p["sensor (A2-A3)"].startswith("! ") and "A2-A3/PB0" in p["sensor (A2-A3)"])
        self.assertTrue(p["radio (A4-A5)"].startswith("! ") and "STUCK" in p["radio (A4-A5)"])
        self.assertTrue(p["estop (B1)"].startswith("! ") and "ON" in p["estop (B1)"])
        self.assertTrue(p["vdd"].startswith("! ") and "2900 mV" in p["vdd"])
        self.assertTrue(p["loopback (A0-A1)"].startswith("+ "))
        self.assertIn("! 12:00:01.000 LW HEALTH 8", f)
        f = once("--db", self.db, "--log", os.path.join(self.tmp.name, "serial.log"))
        self.assertIn("\x1b[31m! sensor (A2-A3)", f)
        self.assertIn("\x1b[32m+ loopback (A0-A1)", f)

    def test_old_format_unknown(self):
        p = panel(self.log("LW HEALTH 3 loopback=OK"))
        self.assertTrue(p["loopback (A0-A1)"].startswith("+ "))
        for k in ("sensor (A2-A3)", "radio (A4-A5)", "estop (B1)", "vdd", "temp"):
            self.assertTrue(p[k].startswith("  ") and "unknown" in p[k], p[k])

    def test_no_health_yet(self):
        self.assertIn("BOARD (latest LW HEALTH: none yet)", self.log("LW BOOT fw=abc"))

    def test_panel_from_events_table(self):
        run_emit(self.db, "jumper")
        c = self.rw()
        with c:
            c.execute("INSERT INTO events (t_wall, board, kind, fields, raw_ts) VALUES "
                      "(?, 'nucleo-1', 'HEALTH', ?, '12:00:09.000')",
                      (time.time(), json.dumps({"line": "LW HEALTH 9 loopback=OK sensor=OK "
                                                        "radio=OPEN estop=OFF vdd=3310"})))
        c.close()
        f = self.frame()
        p = panel(f)
        self.assertTrue(p["radio (A4-A5)"].startswith("! ") and "A4-A5/PC0" in p["radio (A4-A5)"])
        self.assertTrue(p["vdd"].startswith("+ "))
        self.assertIn("! 12:00:09.000 LW HEALTH 9", f)


class DumpAndBannerTest(Base):
    def add_line(self, c, t, line, kind="OTHER"):
        c.execute("INSERT INTO events (t_wall, board, kind, fields, raw_ts) VALUES "
                  "(?, 'nucleo-1', ?, ?, ?)",
                  (t, kind, json.dumps({"line": line}),
                   time.strftime("%H:%M:%S", time.gmtime(t)) + ".500"))

    def test_dump_burst_and_cause_in_banner(self):
        run_emit(self.db, "hardfault")
        c = self.rw()
        inc = c.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 1").fetchone()
        t0 = inc["t_first_bad_line"]
        with c:
            self.add_line(c, t0 - 600, "LW DUMP cause STALEA")
            for line in ("LW DUMP reg r0=0x00000001 r1=0x00000002", "LW DUMP stack bad sp=0x1",
                         "LW DUMP cause CAUSEA CAUSEB", "LW DUMP end"):
                self.add_line(c, t0 - 0.5, line)
        c.close()
        f = self.frame(height=80)
        self.assertIn("cause CAUSEA CAUSEB  CFSR DACCVIOL MMARVALID", f)
        self.assertNotIn("STALEA", f.split("BOARD (")[0])
        for part in ("LW DUMP reg r0", "LW DUMP stack bad", "LW DUMP cause CAUSEA", "LW DUMP end"):
            self.assertTrue(any(l.startswith("! ") and part in l for l in f.splitlines()), part)

    def test_stale_cause_alone_not_shown(self):
        run_emit(self.db, "hardfault")
        c = self.rw()
        inc = c.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 1").fetchone()
        with c:
            self.add_line(c, inc["t_first_bad_line"] - 600, "LW DUMP cause STALEA")
        c.close()
        banner = self.frame().split("BOARD (")[0]
        self.assertIn("CFSR DACCVIOL", banner)
        self.assertNotIn("STALEA", banner)

    def test_cause_from_evidence(self):
        run_emit(self.db, "hardfault")
        c = self.rw()
        with c:
            c.execute("UPDATE incidents SET evidence=?",
                      (json.dumps({"file_line": "src/y.c:3", "dump": {"cause": ["CAUSEZ"]}}),))
        c.close()
        self.assertIn("cause CAUSEZ", self.frame().split("BOARD (")[0])

    def test_device_pins_in_hardware_banner(self):
        run_emit(self.db, "jumper")
        c = self.rw()
        with c:
            c.execute("UPDATE incidents SET evidence=?", (json.dumps(
                {"lines": [{"line": "LW HEALTH 5 loopback=OK sensor=OPEN radio=OK estop=OFF"}]}),))
        f = self.frame()
        self.assertIn(">>> HARDWARE: sensor OPEN on A2-A3/PB0 <<<", f)
        with c:
            c.execute("UPDATE incidents SET evidence=?",
                      (json.dumps({"facts": {"radio": ["STUCK", "STUCK"]}}),))
        c.close()
        self.assertIn(">>> HARDWARE: radio STUCK on A4-A5/PC0 <<<", self.frame())

    def test_diag_and_confirmed_markers(self):
        run_emit(self.db, "jumper")
        c = self.rw()
        inc = c.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 1").fetchone()
        with c:
            for tr, sent in (("diag", inc["opened"] + 1), ("confirmed", None)):
                c.execute("INSERT INTO alerts (incident_id, transition, text, created, sent) "
                          "VALUES (?, ?, 'x', ?, ?)", (inc["id"], tr, inc["opened"], sent))
        f = self.frame()
        row = [l for l in f.splitlines() if l.strip().startswith("#%d " % inc["id"])][0]
        self.assertTrue(row.endswith("[diag]"), row)
        with c:
            c.execute("UPDATE alerts SET sent=? WHERE transition='confirmed'", (inc["opened"] + 2,))
        c.close()
        row = [l for l in self.frame().splitlines()
               if l.strip().startswith("#%d " % inc["id"])][0]
        self.assertTrue(row.endswith("[diag] [confirmed]"), row)


if __name__ == "__main__":
    unittest.main()
