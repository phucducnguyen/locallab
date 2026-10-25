import contextlib
import io
import os
import tempfile
import unittest

from bench import trial_log
from stub import make_db


class TrialLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "bench.db")
        make_db.build(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(trial_log.main(["--db", self.db] + list(args)), 0)
        return out.getvalue().splitlines()

    def test_one_incident(self):
        lines = self.run_main("--incident", "3", "--header")
        self.assertEqual(lines[0], trial_log.HEADER)
        f = lines[1].split("\t")
        self.assertEqual(f[:3], ["3", "HARDFAULT", "CODE"])
        self.assertLess(float(f[4]), 5.0)
        self.assertEqual(f[6], "no explanation yet")

    def test_diagnosis_line_from_explain_alert(self):
        import sqlite3
        c = sqlite3.connect(self.db)
        t = c.execute("SELECT t_first_bad_line FROM incidents WHERE id=1").fetchone()[0]
        c.execute("INSERT INTO alerts (incident_id, transition, text, created, sent) "
                  "VALUES (1, 'explain', ?, ?, ?)",
                  ("why\nDIAGNOSIS: HARDWARE | VERDICT: HEALTH_FAIL | COMMIT: none | CAUSE: jumper",
                   t + 20, t + 21.5))
        c.commit()
        c.close()
        f = self.run_main("--incident", "1")[0].split("\t")
        self.assertEqual(f[5], "21.50")
        self.assertTrue(f[6].startswith("DIAGNOSIS: HARDWARE"))

    def test_since_and_missing(self):
        self.assertEqual(len(self.run_main("--since", "0")), 3)
        self.assertIn("no such incident", self.run_main("--incident", "99")[0])


if __name__ == "__main__":
    unittest.main()
