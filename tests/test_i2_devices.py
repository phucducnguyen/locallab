"""Lane I2: one incident per HEALTH device (sensor, radio, power, estop), triage and alerts.

Synthetic, timestamped lines in the v2 HEALTH grammar; shas are invented.
"""

import json
import unittest

from bench import parser
from bench.alerts import alert_text
from bench.incidents import Incident, Machine
from bench.triage import PassHistory, judge_incident, route_for

SHA = "a0f0c9f"
NEW = "7777777"
OK_ALL = {"sensor": "OK", "radio": "OK", "estop": "OFF"}


class Feed:
    def __init__(self):
        self.m = Machine("nucleo-1")
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

    def health(self, t, n, loopback="OK", **dev):
        fields = dict(OK_ALL)
        fields.update(dev)
        extra = "".join(" %s=%s" % (k, v) for k, v in fields.items() if v is not None)
        outs = self.line(t, "LW ALIVE %d" % n)
        outs += self.line(t + 0.01, "LW HEALTH %d loopback=%s%s" % (n, loopback, extra))
        outs += self.add(self.m.tick(t + 0.02))
        return outs

    def seq(self, t, n, states, key, step=1.0, **fixed):
        for s in states:
            kw = dict(fixed)
            kw[key] = s
            self.health(t, n, **kw)
            t += step
            n += 1
        return t, n

    def types(self, typ):
        return [o for o in self.out if o["type"] == typ]


def row(inc, iid=1, triage="HARDWARE", reason="r"):
    """An incidents row as run.py writes it (evidence carries the facts)."""
    ev = {"lines": inc.cites, "facts": inc.facts, "file_line": None}
    return {"id": iid, "board": inc.board, "kind": inc.kind, "triage": triage,
            "route": route_for(triage), "reason": reason, "evidence": json.dumps(ev),
            "flaps": inc.flaps, "duration_s": None}


class TestLinks(unittest.TestCase):
    def check_alone(self, dev, pins, pin_in):
        f = Feed()
        t, n = f.seq(1, 1, ["OK", "OPEN"], dev)
        self.assertEqual(f.types("open"), [])
        t, n = f.seq(t, n, ["OPEN"], dev)
        opens = f.types("open")
        self.assertEqual(len(opens), 1)
        inc = opens[0]["inc"]
        self.assertEqual(inc.kind, "HEALTH_FAIL")
        self.assertEqual(inc.facts["device"], dev)
        self.assertEqual(inc.facts["pins"], pins)
        self.assertEqual(inc.facts["pin_in"], pin_in)
        self.assertEqual(inc.facts[dev], ["OPEN", "OPEN"])
        self.assertEqual(inc.slot, ("HEALTH_FAIL", dev))
        self.assertIn("%s=OPEN" % dev, opens[0]["cite"]["line"])
        self.assertAlmostEqual(inc.t_first_bad, 2.01)
        t, n = f.seq(t, n, ["OK", "OK"], dev)
        self.assertEqual(f.types("resolve"), [])
        f.seq(t, n, ["OK"], dev)
        res = f.types("resolve")
        self.assertEqual(len(res), 1)
        self.assertIs(res[0]["inc"], inc)
        self.assertAlmostEqual(res[0]["duration"], 6.01 - 3.01, places=3)
        self.assertNotIn("HEALTH_FAIL", f.m.active)   # the loopback slot is untouched
        return inc

    def test_sensor_alone(self):
        self.check_alone("sensor", "A2-A3", "PB0")

    def test_radio_alone(self):
        self.check_alone("radio", "A4-A5", "PC0")

    def test_two_pulled_wires_two_incidents(self):
        f = Feed()
        for i in range(3):
            f.health(1 + i, 1 + i, sensor="OPEN", radio="OPEN")
        opens = f.types("open")
        self.assertEqual(len(opens), 2)
        self.assertEqual({o["inc"].facts["device"] for o in opens}, {"sensor", "radio"})
        self.assertNotEqual(opens[0]["inc"].key, opens[1]["inc"].key)
        for i in range(3):
            f.health(5 + i, 5 + i)
        self.assertEqual(len(f.types("resolve")), 2)

    def test_loopback_and_sensor_are_separate(self):
        f = Feed()
        f.health(1, 1, loopback="OPEN", sensor="OPEN")
        f.health(2, 2, loopback="OPEN", sensor="OPEN")
        opens = f.types("open")
        self.assertEqual(len(opens), 2)
        lb = [o["inc"] for o in opens if "device" not in o["inc"].facts]
        self.assertEqual(len(lb), 1)
        self.assertEqual(lb[0].slot, "HEALTH_FAIL")

    def test_sensor_flapping_1s_apart(self):
        f = Feed()
        t, n = f.seq(1, 1, ["OK", "OK"], "sensor")
        t, n = f.seq(t, n, ["OPEN", "OPEN", "OK", "OPEN", "OK", "OPEN", "OK", "OPEN",
                            "OK", "OPEN"], "sensor")
        t, n = f.seq(t, n, ["OK"] * 3, "sensor")
        self.assertEqual(len(f.types("open")), 1)
        self.assertEqual(len(f.types("resolve")), 1)
        # Pulled again within 60 s: the same incident, FLAPPING, flaps counted.
        for k in range(4):
            t, n = f.seq(t, n, ["OPEN", "OPEN", "OK", "OK", "OK"], "sensor")
        self.assertEqual(len(f.types("open")), 1)
        flaps = f.types("flap")
        self.assertEqual(len(flaps), 4)
        inc = f.types("open")[0]["inc"]
        self.assertTrue(all(o["inc"] is inc for o in flaps))
        self.assertEqual(inc.flaps, 4)
        self.assertEqual([o["alert"] for o in flaps], [True, False, False, False])
        self.assertGreater(inc.repeats, 0)

    def test_stuck(self):
        f = Feed()
        f.seq(1, 1, ["STUCK", "STUCK"], "radio")
        inc = f.types("open")[0]["inc"]
        self.assertEqual(inc.facts["state"], "STUCK")
        self.assertEqual(inc.facts["radio"], ["STUCK", "STUCK"])
        tri, why = judge_incident(inc.kind, inc.facts, inc.cites, SHA, PassHistory([(SHA, 0.0)]))
        self.assertEqual(tri, "HARDWARE")
        self.assertEqual(why, "Radio link STUCK (A5 shorted to ground) on 2 lines in a row on "
                              "a0f0c9f, which passed with all links OK; no reflash since.")
        lines = alert_text("open", row(inc)).splitlines()
        self.assertEqual(lines[0], "🔴 Problem on test board 1: the radio wire (A4–A5) is "
                                   "shorted to ground.")
        self.assertTrue(lines[2].startswith("Details: radio link STUCK (A5 to ground) · "))

    def test_old_format_lines_open_nothing(self):
        f = Feed()
        for i in range(6):
            f.line(1 + i, "LW ALIVE %d" % (i + 1))
            f.line(1.01 + i, "LW HEALTH %d loopback=OK" % (i + 1))
            f.add(f.m.tick(1.02 + i))
        self.assertEqual(f.types("open"), [])
        self.assertEqual(f.m.last_power["vdd"], None)

    def test_absent_field_is_neither_ok_nor_bad(self):
        f = Feed()
        f.health(1, 1, sensor="OPEN")
        f.health(2, 2, sensor=None)           # unknown: the bad run is not broken
        f.health(3, 3, sensor="OPEN")
        self.assertEqual(len(f.types("open")), 1)
        f.health(4, 4, sensor="OK")
        f.health(5, 5, sensor="OK")
        f.health(6, 6, sensor=None)           # unknown: not a third OK
        self.assertEqual(f.types("resolve"), [])
        f.health(7, 7, sensor="OK")
        self.assertEqual(len(f.types("resolve")), 1)


class TestEstop(unittest.TestCase):
    def test_press_and_release(self):
        f = Feed()
        f.health(1, 1)
        out = f.health(2, 2, estop="ON")
        opens = [o for o in out if o["type"] == "open"]
        self.assertEqual(len(opens), 1)
        inc = opens[0]["inc"]
        self.assertEqual(inc.kind, "ESTOP")
        self.assertEqual(inc.facts["device"], "estop")
        f.health(3, 3, estop="ON")
        self.assertEqual(len(f.types("open")), 1)
        self.assertEqual(inc.repeats, 1)
        f.health(4, 4, estop="OFF")
        res = f.types("resolve")
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0]["duration"], 2.0, places=3)
        tri, why = judge_incident("ESTOP", inc.facts, inc.cites, SHA, PassHistory())
        self.assertEqual(tri, "HARDWARE")
        self.assertTrue(why.startswith("emergency stop pressed on board nucleo-1"))
        self.assertIn("operator action, not a fault", why)
        self.assertEqual(judge_incident("ESTOP", inc.facts, inc.cites, None, PassHistory())[0],
                         "HARDWARE")
        lines = alert_text("open", row(inc)).splitlines()
        self.assertEqual(lines[0], "⛔ Someone pressed the emergency stop on test board 1. "
                                   "Not a fault.")
        self.assertTrue(lines[1].startswith("Details: emergency stop pressed (operator action)"))


class TestPower(unittest.TestCase):
    def test_vdd_low_twice_then_recovers(self):
        f = Feed()
        f.health(1, 1, vdd=3300, temp=31)
        f.health(2, 2, vdd=2950)
        self.assertEqual(f.types("open"), [])
        f.health(3, 3, vdd=2950)
        opens = f.types("open")
        self.assertEqual(len(opens), 1)
        inc = opens[0]["inc"]
        self.assertEqual((inc.kind, inc.facts["device"]), ("HEALTH_FAIL", "power"))
        self.assertEqual(inc.facts["vdd"], [2950, 2950])
        self.assertEqual(inc.facts["power"]["vdd"], 2950)
        self.assertEqual(inc.facts["power"]["temp"], 31)   # kept from the earlier line
        self.assertEqual(inc.evidence["power"]["vdd"], 2950)
        tri, why = judge_incident(inc.kind, inc.facts, inc.cites, SHA, PassHistory())
        self.assertEqual(tri, "HARDWARE")
        self.assertIn("vdd 2950 then 2950 mV on 2 lines in a row (below 3000 mV)", why)
        lines = alert_text("open", row(inc)).splitlines()
        self.assertEqual(lines[0], "🔴 Test board 1 is getting too little power (2.95 V).")
        self.assertTrue(lines[2].startswith("Details: power low (vdd 2950 mV) · "))
        f.health(4, 4, vdd=3000)
        f.health(5, 5)                       # no vdd: unknown, not counted
        f.health(6, 6, vdd=3100)
        self.assertEqual(f.types("resolve"), [])
        f.health(7, 7, vdd=3200)
        self.assertEqual(len(f.types("resolve")), 1)

    def test_boot_with_bor(self):
        f = Feed()
        f.health(1, 1)
        out = f.line(2, "LW BOOT fw=%s reset=BOR,PIN csr=0x0c000000" % SHA)
        power = [o for o in out if o["type"] == "open" and o["inc"].facts.get("device") == "power"]
        self.assertEqual(len(power), 1)
        inc = power[0]["inc"]
        self.assertEqual(inc.facts["cause"], "BOR")
        tri, why = judge_incident(inc.kind, inc.facts, inc.cites, None, PassHistory())
        self.assertEqual((tri, route_for(tri)), ("HARDWARE", "Hardware team"))
        self.assertIn("brown-out reset (reset=BOR,PIN)", why)
        lines = alert_text("open", row(inc)).splitlines()
        self.assertEqual(lines[0], "🔴 Test board 1 reset because its power supply dipped "
                                   "too low.")
        self.assertTrue(lines[2].startswith("Details: power brown-out reset (BOR) · "))

    def test_last_power_updates_only_known_values(self):
        f = Feed()
        f.health(1, 1, vdd=3300, temp=30)
        f.health(2, 2, temp=33)
        lp = f.m.last_power
        self.assertEqual((lp["vdd"], lp["temp"], lp["t"]), (3300, 33, 2.01))
        self.assertIn("temp=33", lp["cite"]["line"])
        f.health(3, 3, sensor="OPEN")
        f.health(4, 4, sensor="OPEN")
        inc = f.types("open")[0]["inc"]
        self.assertEqual(inc.facts["power"]["vdd"], 3300)
        self.assertEqual(f.m.extras(inc)["power"]["temp"], 33)
        self.assertEqual(f.m.extras(inc)["device"], "sensor")


class TestTriageAndAlerts(unittest.TestCase):
    def sensor_inc(self):
        f = Feed()
        f.seq(1, 1, ["OPEN", "OPEN"], "sensor")
        return f.types("open")[0]["inc"]

    def test_sensor_hardware_on_passing_sha(self):
        inc = self.sensor_inc()
        tri, why = judge_incident(inc.kind, inc.facts, inc.cites, SHA, PassHistory([(SHA, 0.0)]))
        self.assertEqual((tri, route_for(tri)), ("HARDWARE", "Hardware team"))
        self.assertEqual(why, "Sensor link OPEN (A2-A3 wire) on 2 lines in a row on a0f0c9f, "
                              "which passed with all links OK; no reflash since.")

    def test_sensor_first_run_of_new_sha_is_unknown(self):
        inc = self.sensor_inc()
        tri, why = judge_incident(inc.kind, inc.facts, inc.cites, NEW, PassHistory([(SHA, 0.0)]))
        self.assertEqual(tri, "UNKNOWN")
        self.assertIn("confined to the links", why)
        self.assertIn(SHA, why)

    def test_sensor_unverified_is_unknown(self):
        inc = self.sensor_inc()
        self.assertEqual(judge_incident(inc.kind, inc.facts, inc.cites, None, PassHistory())[0],
                         "UNKNOWN")

    def test_first_alert_head_and_fixed_lines(self):
        inc = self.sensor_inc()
        text = alert_text("open", row(inc, iid=7)).splitlines()
        self.assertEqual(text[0], "🔴 Problem on test board 1: the sensor wire (A2–A3) is "
                                  "disconnected.")
        self.assertEqual([l.split(":")[0] for l in text[1:]], ["Who", "Details"])
        self.assertTrue(text[2].startswith("Details: sensor link OPEN (A2–A3 wire) · "))
        self.assertTrue(text[2].endswith("· #7"))
        self.assertEqual(alert_text("flap-1", row(inc, iid=7)).splitlines()[1],
                         "#7 · sensor link (A2–A3) flapping")

    def test_loopback_head_unchanged(self):
        f = Feed()
        f.health(1, 1, loopback="OPEN")
        f.health(2, 2, loopback="OPEN")
        inc = f.types("open")[0]["inc"]
        self.assertEqual(alert_text("open", row(inc)).splitlines()[0],
                         "🔴 Problem on test board 1: the test wire (A0–A1) is disconnected.")


class TestHardfaultDump(unittest.TestCase):
    DUMP = {"registers": {"r0": "0x0", "pc": "0x8000100"}, "stack": ["0x8000200"],
            "cause_names": ["X"]}

    def fault(self, f, t, dump=None):
        rec = parser.parse_line("LW HARDFAULT pc=0x08000100 lr=0x08000200")["records"][0]
        if dump is not None:
            rec["dump"] = dump
        cite = {"event_id": 99, "raw_ts": "t", "line": rec["line"]}
        return f.add(f.m.record(t, rec, cite))

    def test_dump_into_evidence(self):
        f = Feed()
        self.fault(f, 1, self.DUMP)
        inc = f.types("open")[0]["inc"]
        self.assertEqual(inc.facts["dump"], self.DUMP)
        self.assertEqual(inc.evidence["dump"], self.DUMP)
        self.assertEqual(f.m.extras(inc)["dump"], self.DUMP)

    def test_flap_without_dump_keeps_it(self):
        f = Feed()
        self.fault(f, 1, self.DUMP)
        f.line(2, "LW BOOT fw=%s reset=PIN csr=0x04000000" % SHA)
        self.fault(f, 3)                      # within 60 s: a flap, no dump this time
        self.assertEqual(len(f.types("flap")), 1)
        inc = f.types("open")[0]["inc"]
        self.assertEqual(inc.facts["dump"], self.DUMP)

    def test_no_dump_no_key(self):
        f = Feed()
        self.fault(f, 1)
        self.assertNotIn("dump", f.types("open")[0]["inc"].facts)


class TestRestore(unittest.TestCase):
    def test_reloaded_sensor_incident_keeps_its_slot(self):
        m = Machine("nucleo-1")
        inc = Incident(0, "nucleo-1", "HEALTH_FAIL", 1.0, 1.0, None,
                       {"device": "sensor", "pins": "A2-A3"})
        m.restore(inc)
        self.assertIn(("HEALTH_FAIL", "sensor"), m.active)
        self.assertNotIn("HEALTH_FAIL", m.active)


if __name__ == "__main__":
    unittest.main()
