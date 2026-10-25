import contextlib
import io
import json
import os
import tempfile
import unittest

from stub import emit, make_db
from stub.db import connect


def run_emit(db, scenario):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = emit.main(["--db", db, "--scenario", scenario, "--speed", "0"])
    return code, out.getvalue().strip()


class StubEmitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "bench.db")
        make_db.build(self.db)
        self.c = connect(self.db)
        self.cursor = self.c.execute("SELECT MAX(id) FROM events").fetchone()[0]

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def new_events(self):
        rows = self.c.execute("SELECT * FROM events WHERE id > ? ORDER BY id",
                              (self.cursor,)).fetchall()
        return [(r["kind"], json.loads(r["fields"])) for r in rows]

    def incident(self, iid):
        return self.c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()

    def test_jumper_then_recover(self):
        code, out = run_emit(self.db, "jumper")
        self.assertEqual(code, 0)
        iid = int(out)
        kinds = [k for k, _ in self.new_events()]
        self.assertIn("INCIDENT_OPEN", kinds)
        self.assertIn("ALERT_SENT", kinds)
        inc = self.incident(iid)
        self.assertEqual((inc["state"], inc["triage"], inc["route"]),
                         ("OPEN", "HARDWARE", "Hardware team"))
        self.assertIsNotNone(self.c.execute("SELECT 1 FROM routing WHERE incident_id=?",
                                            (iid,)).fetchone())
        code, out = run_emit(self.db, "recover")
        self.assertEqual((code, int(out)), (0, iid))
        inc = self.incident(iid)
        self.assertEqual(inc["state"], "RESOLVED")
        self.assertIsNotNone(inc["duration_s"])
        transitions = [r[0] for r in self.c.execute(
            "SELECT transition FROM alerts WHERE incident_id=? ORDER BY id", (iid,))]
        self.assertEqual(transitions, ["open", "resolve"])

    def test_recover_with_nothing_open_fails(self):
        self.c.execute("UPDATE incidents SET state='RESOLVED'")
        self.c.commit()
        self.assertEqual(run_emit(self.db, "recover")[0], 1)

    def test_hardfault(self):
        code, out = run_emit(self.db, "hardfault")
        self.assertEqual(code, 0)
        inc = self.incident(int(out))
        self.assertEqual((inc["kind"], inc["triage"], inc["route"]),
                         ("HARDFAULT", "CODE", "Firmware team"))
        events = self.new_events()
        self.assertIn("RUN_END", [k for k, _ in events])
        run_id = [f["run_id"] for k, f in events if k == "RUN_END"][0]
        routing = self.c.execute("SELECT * FROM routing WHERE incident_id=?",
                                 (inc["id"],)).fetchone()
        self.assertEqual(routing["run_id"], run_id)

    def test_flap(self):
        code, out = run_emit(self.db, "flap")
        self.assertEqual(code, 0)
        inc = self.incident(int(out))
        self.assertEqual((inc["state"], inc["flaps"]), ("FLAPPING", 4))
        flap_events = [f for k, f in self.new_events() if k == "INCIDENT_FLAP"]
        self.assertEqual([f["flaps"] for f in flap_events], [1, 2, 3, 4])
        transitions = [r[0] for r in self.c.execute(
            "SELECT transition FROM alerts WHERE incident_id=? ORDER BY id", (inc["id"],))]
        self.assertEqual(transitions, ["open", "flap-1"])

    def test_works_on_an_empty_db(self):
        empty = os.path.join(self.tmp.name, "empty.db")
        code, out = run_emit(empty, "jumper")
        self.assertEqual(code, 0)
        self.assertEqual(out, "1")


if __name__ == "__main__":
    unittest.main()
