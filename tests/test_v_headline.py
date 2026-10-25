"""Lane V: the plain-English headline, the stale banner and the timeline fields."""

import time
import unittest

from bench import dashboard
from stub.db import Writer
from tests.test_v_dashboard import Base, add_health, ok_fields


class HeadlineTest(Base):
    def fresh(self, conn, **over):
        f, line = ok_fields(**over)
        add_health(conn, time.time(), line, **f)

    def open(self, conn, kind, triage, ev, alert_after=1.9, ticket=None):
        t = time.time()
        iid = Writer(conn).open_incident(t, kind, triage, "x", ev, t_first_bad=t)
        conn.execute("UPDATE incidents SET t_alert_sent=?, ticket=? WHERE id=?",
                     (t + alert_after, ticket, iid))
        conn.commit()
        return iid

    def test_all_clear(self):
        conn = self.empty()
        self.fresh(conn)
        conn.close()
        st = self.state()
        self.assertEqual(st["headline"]["text"], "✅ Everything is working")
        self.assertEqual(st["headline"]["level"], "ok")
        self.assertFalse(st["stale"])
        self.assertIsNone(st["stopwatch"])

    def test_sensor_open(self):
        conn = self.empty()
        self.fresh(conn, sensor="OPEN")
        iid = self.open(conn, "HEALTH_FAIL", "HARDWARE",
                        {"device": "sensor", "fields": {"sensor": "OPEN"}}, ticket="3")
        conn.close()
        st = self.state()
        h = st["headline"]
        self.assertEqual(h["text"], "\U0001f534 Board 1: the sensor wire came loose.")
        self.assertEqual(h["level"], "bad")
        self.assertEqual(h["sub"], "Team: hardware · alert sent in 1.9 s · "
                                   "incident #%d" % iid)
        self.assertEqual(st["stopwatch"]["ticket"], "3")
        self.assertEqual(st["stopwatch"]["alert_s"], 1.9)

    def test_hardfault(self):
        conn = self.empty()
        self.fresh(conn)
        self.open(conn, "HARDFAULT", "CODE", {"file_line": "src/demo.c:42"})
        conn.close()
        h = self.state()["headline"]
        self.assertEqual(h["text"], "\U0001f534 Board 1: the new code made it crash.")
        self.assertTrue(h["sub"].startswith("Team: software"))

    def test_estop(self):
        conn = self.empty()
        self.fresh(conn, estop="ON")
        self.open(conn, "ESTOP", "HARDWARE", {"device": "estop"})
        conn.close()
        h = self.state()["headline"]
        self.assertEqual(h["text"],
                         "⛔ Board 1: someone pressed the stop button. Not a fault.")
        self.assertEqual(h["level"], "stop")

    def test_silent_unknown(self):
        conn = self.empty()
        self.fresh(conn)
        self.open(conn, "SILENT", "UNKNOWN", {})
        conn.close()
        h = self.state()["headline"]
        self.assertEqual(h["text"], "\U0001f7e1 Board 1 stopped talking after the update. "
                                    "Checking if it is the board or the code.")
        self.assertEqual(h["level"], "warn")

    def test_stale_banner(self):
        conn = self.empty()
        f, line = ok_fields()
        add_health(conn, time.time() - 6.0, line, **f)
        conn.close()
        st = self.state()
        self.assertTrue(st["stale"])
        self.assertEqual(st["headline"]["text"], "Waiting for the board…")
        self.assertEqual(st["headline"]["level"], "unknown")

    def test_no_health_and_missing_db(self):
        self.empty().close()
        st = self.state()
        self.assertTrue(st["stale"])
        self.assertEqual(st["headline"]["level"], "unknown")
        self.tmp.cleanup()
        st = self.state()
        self.assertEqual(st["headline"]["text"], "Waiting for the board…")

    def test_first_sentence(self):
        fs = dashboard.first_sentence
        self.assertEqual(fs("LOCAL LAB incident 5 explanation\nThe sensor wire is "
                            "unplugged. More text here.\nIncident: 5"),
                         "The sensor wire is unplugged.")
        self.assertIsNone(fs(None))
        self.assertEqual(dashboard.board_name("nucleo-1"), "Board 1")
        self.assertEqual(dashboard.board_name(None), "The board")


if __name__ == "__main__":
    unittest.main()
