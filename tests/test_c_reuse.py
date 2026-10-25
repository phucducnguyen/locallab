"""Lane C (C4): the ticket link alert, and one issue per problem across incidents."""

import json
import os
import unittest
from unittest import mock

from stub.db import Writer, connect as stub_connect, init as stub_init
from tests.test_c_mirror import Base
from tracker import mirror

T0 = 1793000000.0  # simulated wall clock (seconds)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class ReuseBase(Base):
    lag = 0.3

    def setUp(self):
        super().setUp()
        conn = stub_connect(self.db)
        stub_init(conn)
        conn.close()
        self.do_setup()
        self.clock = Clock(T0)
        env = mock.patch.dict(os.environ, {"LW_TRACKER_PUBLIC": "http://lab.example:3000/"})
        env.start()
        self.addCleanup(env.stop)

    def writer(self, board="nucleo-1"):
        conn = stub_connect(self.db)
        self.addCleanup(conn.close)
        return Writer(conn, board=board)

    def pull(self, device, state="OPEN", resolve_after=30.0, board="nucleo-1"):
        """A device pulled at the clock's time: an incident, its open alert, then resolved."""
        w = self.writer(board)
        t = self.clock.t
        ev = {"device": device, "state": state, device: [state, state]}
        iid = w.open_incident(t, "HEALTH_FAIL", "HARDWARE", "%s=%s on 2 lines" % (device, state),
                              ev)
        w.alert(t, iid, "open")
        if resolve_after is not None:
            w.resolve(t + resolve_after, iid)
        return iid

    def code(self, file_line, resolve_after=None):
        w = self.writer()
        t = self.clock.t
        iid = w.open_incident(t, "HARDFAULT", "CODE", "fault at %s" % file_line,
                              {"file_line": file_line})
        w.alert(t, iid, "open")
        if resolve_after is not None:
            w.resolve(t + resolve_after, iid)
        return iid

    def run_mirror(self, advance=0.0):
        """One pass at the clock (after the flap window has passed)."""
        self.clock.t += advance
        return self.mirror(wait=1.5, step=0.1, wall=self.clock).once()

    def tickets(self, iid):
        return self.rows("SELECT * FROM alerts WHERE incident_id=? AND transition='ticket'", iid)

    def ticket(self, iid):
        return int(self.rows("SELECT ticket FROM incidents WHERE id=?", iid)[0]["ticket"])

    def issue(self, number):
        return [i for i in self.fake.issues() if i["number"] == number][0]

    def again(self, number):
        return [c for c in self.issue(number)["comments"] if c.startswith("happened again at ")]


class TicketAlertTest(ReuseBase):
    def test_ticket_row_once_with_url_reply_to_open(self):
        iid = self.pull("sensor", resolve_after=None)
        self.run_mirror()
        rows = self.tickets(iid)
        self.assertEqual(len(rows), 1)
        n = self.ticket(iid)
        self.assertEqual(rows[0]["text"],
                         "Ticket #%d: http://lab.example:3000/labwatch/bench-1/issues/%d" % (n, n))
        opened = self.rows("SELECT id FROM alerts WHERE incident_id=? AND transition='open'", iid)
        self.assertEqual(rows[0]["reply_to_alert_id"], opened[0]["id"])
        self.assertIsNone(rows[0]["sent"])
        self.run_mirror(5.0)
        self.run_mirror(5.0)
        self.assertEqual(len(self.tickets(iid)), 1)

    def test_default_base(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("LW_TRACKER_PUBLIC", None)
            self.assertEqual(mirror.ticket_text(7),
                             "Ticket #7: http://127.0.0.1:3000/labwatch/bench-1/issues/7")


class ProblemKeyTest(unittest.TestCase):
    def row(self, kind="HEALTH_FAIL", triage="HARDWARE", ev=None, reason="r", board="3"):
        return {"incident_id": 1, "board": board, "kind": kind, "triage": triage,
                "reason": reason, "i_evidence": json.dumps(ev or {})}

    def test_formats(self):
        k = mirror.problem_key
        self.assertEqual(k(self.row(ev={"device": "sensor", "state": "STUCK"})),
                         "board3/sensor/STUCK")
        self.assertEqual(k(self.row(ev={"device": "radio", "radio": ["OPEN", "OPEN"]})),
                         "board3/radio/OPEN")
        self.assertEqual(k(self.row(ev={"loopback": ["OPEN", "STUCK"]})), "board3/loopback/STUCK")
        self.assertEqual(k(self.row(reason="loopback=OPEN on 2 lines")), "board3/loopback/OPEN")
        self.assertEqual(k(self.row(ev={"device": "power", "cause": "VDD_LOW"})),
                         "board3/power/LOW")
        self.assertEqual(k(self.row(kind="LINK_DOWN",
                                    ev={"device": "estop"})), "board3/estop/ON")
        self.assertEqual(k(self.row(kind="SILENT", triage="UNKNOWN")), "board3/SILENT/FAIL")
        self.assertEqual(k(self.row(kind="HARDFAULT", triage="CODE",
                                    ev={"file_line": "src/a.c:12"})), "board3/CODE/src/a.c:12")
        self.assertEqual(k(self.row(kind="HARDFAULT", triage="CODE")), "board3/CODE/HARDFAULT")
        self.assertIsNone(k({"incident_id": None}))


class ReuseTest(ReuseBase):
    def test_three_sensor_pulls_one_issue_then_radio_then_late_sensor(self):
        a = self.pull("sensor")
        self.run_mirror(60.0)
        b_t = self.clock.t + 240.0
        self.clock.t = b_t
        b = self.pull("sensor")
        self.run_mirror(60.0)
        self.clock.t += 240.0
        c = self.pull("sensor", resolve_after=None)  # still open when mirrored
        self.run_mirror(5.0)
        n = self.ticket(a)
        self.assertEqual((self.ticket(b), self.ticket(c)), (n, n))
        self.assertEqual(len(self.fake.issues()), 1)
        issue = self.issue(n)
        self.assertIn("lw-key: boardnucleo-1/sensor/OPEN", issue["body"].splitlines())
        again = self.again(n)
        self.assertEqual(len(again), 2, issue["comments"])
        self.assertTrue(again[0].startswith("happened again at %s, incident %d, duration 30 s"
                                            % (mirror.utc(b_t), b)), again[0])
        self.assertIn("incident %d, duration still open" % c, again[1])
        self.assertEqual(issue["state"], "open")  # c is still open
        # Routing rows carry the shared number too.
        self.assertEqual({r["ticket"] for r in self.rows("SELECT ticket FROM routing")}, {str(n)})
        # One ticket row per incident, each pointing at the shared issue.
        for iid in (a, b, c):
            rows = self.tickets(iid)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["text"].endswith("/issues/%d" % n))
        # A radio pull is another problem.
        r = self.pull("radio", resolve_after=None)
        self.run_mirror(5.0)
        self.assertNotEqual(self.ticket(r), n)
        self.assertEqual(len(self.fake.issues()), 2)
        self.assertEqual(self.again(self.ticket(r)), [])
        # c resolves: the last open incident on the sensor issue, so it closes now.
        self.writer().resolve(self.clock.t, c)
        self.run_mirror(5.0)
        self.assertEqual(self.issue(n)["state"], "closed")
        # 70 minutes after that close: a new sensor issue.
        self.clock.t += 70 * 60
        d = self.pull("sensor")
        self.run_mirror(60.0)
        self.assertNotIn(self.ticket(d), (n, self.ticket(r)))
        self.assertEqual(len(self.fake.issues()), 3)
        self.assertEqual(len(self.again(n)), 2)

    def test_closed_recently_is_reopened(self):
        a = self.pull("sensor")
        self.run_mirror(60.0)
        n = self.ticket(a)
        self.assertEqual(self.issue(n)["state"], "closed")
        self.clock.t += 50 * 60
        b = self.pull("sensor", resolve_after=None)
        self.run_mirror(5.0)
        self.assertEqual(self.ticket(b), n)
        self.assertEqual(self.issue(n)["state"], "open")
        self.assertEqual(len(self.again(n)), 1)
        self.assertEqual(len(self.fake.issues()), 1)

    def test_shared_issue_closes_with_last_incident(self):
        a = self.pull("sensor", resolve_after=None)
        self.run_mirror(5.0)
        b = self.pull("sensor", board="nucleo-1", resolve_after=None)
        self.run_mirror(5.0)
        n = self.ticket(a)
        self.assertEqual(self.ticket(b), n)
        self.writer().resolve(self.clock.t, a)
        self.run_mirror(5.0)
        self.assertEqual(self.issue(n)["state"], "open")  # b is still open
        self.writer().resolve(self.clock.t, b)
        self.run_mirror(5.0)
        self.assertEqual(self.issue(n)["state"], "closed")

    def test_code_fault_reuses_open_issue_by_file_line(self):
        a = self.code("src/demo.c:42")
        self.run_mirror(5.0)
        b = self.code("src/demo.c:42")
        other = self.code("src/demo.c:77")
        self.run_mirror(5.0)
        n = self.ticket(a)
        self.assertEqual(self.ticket(b), n)
        self.assertNotEqual(self.ticket(other), n)
        self.assertIn("lw-key: boardnucleo-1/CODE/src/demo.c:42",
                      self.issue(n)["body"].splitlines())
        self.assertEqual(len(self.again(n)), 1)
        self.assertEqual(len(self.fake.issues()), 2)
        self.assertEqual(self.issue(n)["labels"][0]["name"], "firmware")

    def test_crash_between_create_and_store_one_issue(self):
        a = self.pull("sensor", resolve_after=None)
        self.fake.drop_reply_once = True  # created, reply lost: the number never stored
        self.assertEqual(self.run_mirror(5.0), [])
        self.assertEqual(len(self.fake.issues()), 1)
        b = self.pull("sensor", resolve_after=None)  # same problem while the create is unknown
        # Restart with a fresh Mirror: the search lags; exact title + marker line found.
        done = self.run_mirror(5.0)
        self.assertIn(("lw-%d" % a, "create", 1, "found"), done)
        self.assertEqual(len(self.fake.issues()), 1)
        self.assertEqual((self.ticket(a), self.ticket(b)), (1, 1))
        self.assertEqual(len(self.again(1)), 1)
        self.assertEqual(len(self.tickets(a)), 1)
        self.assertEqual(len(self.tickets(b)), 1)

    def test_restart_creates_nothing(self):
        self.pull("sensor")
        self.run_mirror(60.0)
        self.pull("sensor", resolve_after=None)
        self.run_mirror(5.0)
        n_calls = len(self.fake.calls)
        self.assertEqual(self.run_mirror(5.0), [])
        self.assertEqual([c for c in self.fake.calls[n_calls:] if c[0] != "GET"], [])


if __name__ == "__main__":
    unittest.main()
