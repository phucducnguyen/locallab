"""Lane B: check_citations on good and bad explanations of the stub's records."""

import io
import json
import os
import subprocess
import sys
import tempfile

from agent import check_citations as cc
from tests.test_b_client import ROOT, StubFixture

GOOD_HARDFAULT = """What the board did: the new build faulted right after two passing self-tests [serial 09:40:02.150].
The fault decode shows a data access violation [cfsr DACCVIOL; hfsr FORCED].
Hardware or code: the bench triaged it CODE on its own run of 2222222 [incident 3].
Likely cause: the commit writes through a pointer computed past the end of the log buffer [diff 2222222 +15]. The faulting store is at the recorded source line [src/demo.c:42].
Next step: likely restore the bounded index, then rerun this sha.
DIAGNOSIS: CODE | VERDICT: HARDFAULT | COMMIT: 2222222 | CAUSE: overflow
"""

GOOD_JUMPER = """- The loopback read OPEN on two lines in a row [serial 09:00:01.000; serial 09:00:02.000].
- The bench's own loopback test then failed [serial 09:00:02.500].
- **Hardware or code:** HARDWARE, on the sha that last passed [incident 1].
DIAGNOSIS: HARDWARE | VERDICT: HEALTH_FAIL | COMMIT: none | CAUSE: jumper
"""


class CitationsTest(StubFixture):
    def fetch(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        from tests.test_b_client import labwatch
        self.assertEqual(labwatch.main(["--config", self.config] + list(argv), out, err), 0)
        return json.loads(out.getvalue())["body"]

    def test_good_hardfault_explanation(self):
        rec, show = self.fetch("incident", "3"), self.fetch("show", "2222222")
        r = cc.check(GOOD_HARDFAULT, rec, show)
        self.assertEqual(r["unresolved"], [])
        # The one uncited sentence is marked "likely"; it is still listed.
        self.assertEqual(len(r["uncited"]), 1)
        self.assertTrue(r["uncited"][0]["likely"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["sentences"], 6)

    def test_fully_cited_jumper_is_ok(self):
        r = cc.check(GOOD_JUMPER, self.fetch("incident", "1"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sentences"], 3)

    def test_run_record(self):
        text = ("The run faulted [serial 09:40:02.150]. It names a source line [src/demo.c:42].\n"
                "The change is in the diff [diff 2222222 +16].\n")
        r = cc.check(text, self.fetch("run", "2"), self.fetch("show", "2222222"))
        self.assertTrue(r["ok"], r)

    def test_bad_citations_are_listed(self):
        rec, show = self.fetch("incident", "3"), self.fetch("show", "2222222")
        text = ("It faulted [serial 23:59:59.999]. The bit was set [cfsr IBUSERR]. "
                "See the source [src/other.c:7]. See the commit [diff 1111111 +3]. "
                "Line out of range [diff 2222222 +999]. Another incident [incident 2]. "
                "Bad form [sic]. Register value form [pc 0x08001234].\n"
                "No citation here at all.\n"
                "DIAGNOSIS: CODE | VERDICT: HARDFAULT | COMMIT: 2222222 | CAUSE: x\n")
        r = cc.check(text, rec, show)
        bad = [u["citation"] for u in r["unresolved"]]
        self.assertEqual(bad, ["serial 23:59:59.999", "cfsr IBUSERR", "src/other.c:7",
                               "diff 1111111 +3", "diff 2222222 +999", "incident 2", "sic",
                               "pc 0x08001234"])
        self.assertEqual([u["sentence"] for u in r["uncited"]], ["No citation here at all."])
        self.assertFalse(r["uncited"][0]["likely"])
        self.assertFalse(r["ok"])

    def test_diff_without_show_checks_the_sha_only(self):
        rec = self.fetch("incident", "3")
        self.assertTrue(cc.check("A change [diff 2222222 +999].", rec)["ok"])
        self.assertFalse(cc.check("A change [diff 1111111 +1].", rec)["ok"])
        self.assertFalse(cc.check("A change [diff 2222222 +1].", rec, {"ref": "1111111",
                                  "data": {"diff": ["x"]}})["ok"])

    def test_last_pass_sha_is_not_the_records_commit(self):
        rec = self.fetch("incident", "3")
        self.assertFalse(cc.check("Old one [diff 1111111 +1].", rec)["ok"])

    def test_sentence_split_keeps_citation_after_the_period(self):
        r = cc.check("The loopback was open. [serial 09:00:01.000]\n", self.fetch("incident", "1"))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sentences"], 1)

    def test_cli(self):
        tmp = tempfile.mkdtemp(dir=self.tmp.name)
        paths = {}
        for name, body in (("rec", {"status": 200, "body": self.fetch("incident", "1")}),):
            paths[name] = os.path.join(tmp, name + ".json")
            with open(paths[name], "w") as f:
                json.dump(body, f)
        for text, rc in ((GOOD_JUMPER, 0), ("Uncited claim.\n", 1)):
            exp = os.path.join(tmp, "exp.txt")
            with open(exp, "w") as f:
                f.write(text)
            p = subprocess.run([sys.executable, "-m", "agent.check_citations", "--explanation",
                                exp, "--record", paths["rec"]], cwd=ROOT, capture_output=True,
                               text=True, timeout=30)
            self.assertEqual(p.returncode, rc, p.stderr)
            self.assertEqual(json.loads(p.stdout)["ok"], rc == 0)
