import json
import re
import unittest

from helpers import FakeBackend, example

from ticket_triage import linear
from ticket_triage.evidence import names_issue, refs_in
from ticket_triage.prepare import MAX_COMMENTS, TextPolicy
from ticket_triage.triage import classify


def issues():
    return {i["identifier"]: i for i in example("issues.example.json")}


def canned(state, questions):
    return {r["id"]: r for r in example("results.example.json")}["ENG-101"]["answers"]


POLICY = TextPolicy(ignore_comments=[re.compile(r'^changed feedback status to ".*" in \w+$', re.I)],
                    cut_description=[re.compile(r"Session details", re.I)])


class WhatLeaves(unittest.TestCase):
    """What goes to the model: checked offline with a fake backend."""

    def run_one(self, iid, **kw):
        fake = FakeBackend(canned)
        row = classify(issues()[iid], fake, example("repository.example.json"), kw.pop("policy", POLICY),
                       with_evidence=False, **kw)
        self.assertEqual(len(fake.calls), 1)
        return row, fake.calls[0][0], fake.calls[0][1]

    def test_defaults(self):
        row, state, questions = self.run_one("ENG-101")
        blob = json.dumps(state)
        self.assertNotIn("labels", state["issue"])
        self.assertNotIn("Example Person", blob)
        self.assertNotIn("_about", blob)
        self.assertNotIn("deploy_includes", state["repository"])
        self.assertNotIn("Session details", state["issue"]["description"])
        self.assertNotIn("feedback status", blob)
        self.assertEqual([c["author"] for c in state["issue"]["comments"]], ["person 1", "person 2", "person 2"])
        self.assertNotIn("code_evidence", state)
        self.assertNotIn("evidence_shows_done", questions)
        self.assertEqual(row["model"], "fake-1")

    def test_opt_ins(self):
        policy = TextPolicy(include_authors=True)
        _, state, _ = self.run_one("ENG-101", policy=policy, include_labels=True)
        self.assertEqual(state["issue"]["labels"], ["Improvement", "Size: S"])
        self.assertIn("Example Person One", json.dumps(state))

    def test_as_of_pickup_cuts_later_comments(self):
        _, state, _ = self.run_one("ENG-101", as_of_pickup=True)
        self.assertEqual([c["text"] for c in state["issue"]["comments"]], ["Agreed, small copy change."])

    def test_comment_window(self):
        _, state, _ = self.run_one("ENG-102")
        self.assertEqual(len(state["issue"]["comments"]), MAX_COMMENTS)
        self.assertTrue(state["issue"]["earlier_comments_omitted"])


class Evidence(unittest.TestCase):
    def test_refs(self):
        text = ("See src/settings/SettingsForm.tsx and `SettingsForm`; trace shows .next/server/chunks/123.js "
                "and node_modules/x/index.js, ab.js, and `x`.")
        paths, symbols = refs_in(text)
        self.assertEqual(paths, ["src/settings/SettingsForm.tsx"])
        self.assertEqual(symbols, ["SettingsForm"])

    def test_issue_id_is_matched_whole(self):
        self.assertTrue(names_issue("Fix export (ENG-12)", "ENG-12"))
        self.assertTrue(names_issue("eng-12-fix-export", "ENG-12"))
        self.assertFalse(names_issue("Fix export (ENG-123)", "ENG-12"))
        self.assertFalse(names_issue("XENG-12", "ENG-12"))


class Ids(unittest.TestCase):
    def test_ids_span_teams(self):
        self.assertEqual(linear.parse_ids(["ENG-1", " ENG-22", "OPS-3", ""]), {"ENG": [1, 22], "OPS": [3]})

    def test_bad_id_exits(self):
        with self.assertRaises(SystemExit):
            linear.parse_ids(["eng 1"])


if __name__ == "__main__":
    unittest.main()


class GitEvidence(unittest.TestCase):
    """CodeEvidence against a throwaway local repository. gh is stubbed; nothing reaches the network."""

    def setUp(self):
        import subprocess
        import tempfile
        self.tmp = tempfile.mkdtemp()
        run = lambda *a: subprocess.run(["git", "-C", self.tmp] + list(a), check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        run("config", "user.email", "test@example.com")
        run("config", "user.name", "Test")
        import os
        os.makedirs(os.path.join(self.tmp, "src", "export"))
        with open(os.path.join(self.tmp, "src", "export", "table.py"), "w") as f:
            f.write("def exportTable():\n    pass\n")
        run("add", "-A")
        run("commit", "-q", "-m", "ENG-7 handle empty tables")
        run("commit", "-q", "--allow-empty", "-m", "ENG-70 unrelated")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def test_gather(self):
        from ticket_triage.evidence import CodeEvidence
        ce = CodeEvidence(base="main", cwd=self.tmp)
        self.assertEqual(ce.base, "main")  # no origin/main here, so the local branch is used
        ce._prs = [{"number": 5, "title": "Handle empty tables (ENG-7)", "state": "MERGED", "headRefName": "x", "mergedAt": "2026-01-01"},
                   {"number": 6, "title": "Other (ENG-77)", "state": "OPEN", "headRefName": "eng-77", "mergedAt": None}]
        issue = {"identifier": "ENG-7", "title": "Export fails", "description": "In export/table.py, `exportTable` and missing.py"}
        ev = ce.gather(issue, [])
        self.assertEqual([(f["name"], f["exists"]) for f in ev["referenced_files"]], [("export/table.py", True), ("missing.py", False)])
        self.assertEqual(ev["referenced_symbols"][0]["found_in_files"], 1)
        self.assertEqual([p["number"] for p in ev["pull_requests"]], [5])
        self.assertEqual(ev["merged_pr_count"], 1)
        self.assertEqual(len(ev["commits"]), 1)
        self.assertIn("ENG-7 handle", ev["commits"][0])
        withheld = ce.gather(issue, [], use_issue_links=False)
        self.assertNotIn("pull_requests", withheld)
