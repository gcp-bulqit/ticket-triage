import json
import os
import shutil
import tempfile
import unittest

from helpers import example

from ticket_triage.triage import emit_triage, rebucket


class EmitTriage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, ".agent", "triage.json")
        self.rows = rebucket(example("results.example.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_probably_pr_is_code_by_default(self):
        added = emit_triage(self.rows, self.path, now="2026-01-01T00:00:00Z")
        self.assertEqual(added, 12)
        t = self.read()
        self.assertEqual(t["ENG-101"]["verdict"], "CODE")
        self.assertEqual(t["ENG-102"]["verdict"], "CODE")
        self.assertEqual(t["ENG-104"]["verdict"], "AMBIGUOUS")
        self.assertEqual(t["ENG-105"]["verdict"], "AMBIGUOUS")
        self.assertEqual(t["ENG-106"]["verdict"], "NOT_CODE")
        self.assertEqual(t["ENG-107"]["verdict"], "NOT_CODE")
        self.assertEqual(t["ENG-101"]["by"], "ticket-triage")
        self.assertIn("pure_pr", t["ENG-101"]["reason"])
        self.assertEqual(sum(v["verdict"] == "CODE" for v in t.values()), 5)

    def test_exclude_probably_pr(self):
        emit_triage(self.rows, self.path, include_probably=False)
        t = self.read()
        self.assertEqual(t["ENG-101"]["verdict"], "CODE")
        self.assertEqual(t["ENG-102"]["verdict"], "AMBIGUOUS")
        self.assertEqual(sum(v["verdict"] == "CODE" for v in t.values()), 1)

    def test_existing_rows_are_never_overwritten(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"ENG-101": {"verdict": "NOT_CODE", "reason": "a person said so"}}, f)
        added = emit_triage(self.rows, self.path)
        self.assertEqual(added, 11)
        self.assertEqual(self.read()["ENG-101"], {"verdict": "NOT_CODE", "reason": "a person said so"})
        self.assertEqual(emit_triage(self.rows, self.path), 0)

    def test_list_form_is_kept(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump([{"id": "ENG-102", "priority": 2, "verdict": "NOT_CODE", "reason": "kept"}], f)
        emit_triage(self.rows, self.path)
        t = self.read()
        self.assertIsInstance(t, list)
        self.assertEqual(len(t), 12)
        self.assertEqual([r for r in t if r["id"] == "ENG-102"], [{"id": "ENG-102", "priority": 2, "verdict": "NOT_CODE", "reason": "kept"}])
        self.assertEqual(next(r for r in t if r["id"] == "ENG-109")["verdict"], "CODE")


if __name__ == "__main__":
    unittest.main()
