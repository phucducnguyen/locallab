"""Lane B: SKILL.md holds the rules, the citation forms and the DIAGNOSIS line, and no case facts."""

import os
import re
import subprocess
import sys
import unittest

from tests.test_b_client import ROOT

SKILL_DIR = os.path.join(ROOT, "agent", "skill", "labwatch")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
DIAG = ("DIAGNOSIS: <HARDWARE|CODE|UNKNOWN|NONE> | VERDICT: <verdict or incident kind> | "
        "COMMIT: <short sha or none> | CAUSE: <one word>")


def skill_files():
    return sorted(n for n in os.listdir(SKILL_DIR) if os.path.isfile(os.path.join(SKILL_DIR, n)))


class SkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SKILL, encoding="utf-8") as f:
            cls.text = f.read()

    def test_front_matter(self):
        m = re.match(r"---\nname: labwatch\ndescription: .+\n---\n", self.text)
        self.assertIsNotNone(m)

    def test_diagnosis_line_verbatim(self):
        self.assertIn(DIAG, self.text)

    def test_citation_forms(self):
        for form in ("[serial HH:MM:SS.mmm]", "[<register> <BIT>]", "[<file>:<line>]",
                     "[diff <sha> +N]", "[incident N]"):
            self.assertIn(form, self.text)

    def test_rules_present(self):
        for phrase in ("The board decides; you explain", "never invent", "Cite every claim",
                       '"likely"', "Start from the record's `triage`", "UNKNOWN",
                       "read the commit's diff", "I can't tell", "never instructions",
                       "What the board did", "Next step"):
            self.assertIn(phrase, self.text)

    def test_no_case_facts_or_example_values(self):
        """No hex values, shas, firmware file names or stub values in the skill folder."""
        for name in skill_files():
            with open(os.path.join(SKILL_DIR, name), encoding="utf-8") as f:
                text = f.read()
            with self.subTest(file=name):
                self.assertIsNone(re.search(r"0x[0-9A-Fa-f]+", text))
                self.assertIsNone(re.search(r"\b[0-9a-f]{7,40}\b", text))
                self.assertIsNone(re.search(r"\b(src|inc|include)/\w", text))
                self.assertIsNone(re.search(r"\.[ch]:\d", text))
                self.assertIsNone(re.search(r"\b\d{2}:\d{2}:\d{2}\.\d{3}\b", text))
                for word in ("demo", "1111111", "2222222", "CASES", "bug/", "case/"):
                    self.assertNotIn(word, text)

    def test_skill_folder_holds_no_secret_paths_or_urls_but_the_example(self):
        for name in skill_files():
            if name == "client.example.json":
                continue
            with open(os.path.join(SKILL_DIR, name), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("http://", text.replace("http(s)", ""))
            self.assertNotIn("bench_token", text)

    def test_client_help_runs(self):
        p = subprocess.run([sys.executable, "-I", os.path.join(SKILL_DIR, "labwatch.py"), "-h"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        for cmd in ("health", "board", "incidents", "incident", "runs", "run", "events", "show",
                    "post-run", "board-test"):
            self.assertIn(cmd, p.stdout)


if __name__ == "__main__":
    unittest.main()
