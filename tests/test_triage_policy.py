import copy
import io
import json
import os
import random
import shutil
import tempfile
import unittest


def read(path):
    with open(path) as f:
        return f.read()
from contextlib import redirect_stdout

from helpers import example, example_path

from ticket_triage import triage
from ticket_triage.triage import (ALREADY_RESOLVED, NEEDS_DECISION, NOT_PR, PR_PLUS_OPS, PROBABLY_PR, PURE_PR,
                                  bucket, rebucket, render_report)

# Written by hand from the v2 policy, not computed by bucket(), so the test can disagree with it.
EXPECTED = {
    "ENG-101": PURE_PR,
    "ENG-102": PROBABLY_PR,
    "ENG-103": PROBABLY_PR,       # path says decision_first, flags lean PR: probably-PR is checked first
    "ENG-104": PR_PLUS_OPS,
    "ENG-105": NEEDS_DECISION,
    "ENG-106": NOT_PR,
    "ENG-107": ALREADY_RESOLVED,  # a comment says so
    "ENG-108": ALREADY_RESOLVED,  # evidence reads done AND a merged PR names it
    "ENG-109": PROBABLY_PR,       # evidence reads done but no merged PR: a reading, not a fact
    "ENG-110": PROBABLY_PR,       # size gate
    "ENG-111": NOT_PR,
    "ENG-112": NOT_PR,            # falls through every rule
}


def pure_answers():
    rows = {r["id"]: r for r in example("results.example.json")}
    return copy.deepcopy(rows["ENG-101"]["answers"])


class BucketPolicy(unittest.TestCase):
    def test_canned_answers(self):
        for r in example("results.example.json"):
            self.assertEqual(bucket(r["answers"], r["evidence"]), EXPECTED[r["id"]], r["id"])

    def test_every_bucket_is_reachable(self):
        self.assertEqual(set(EXPECTED.values()), set(triage.BUCKETS))

    def test_pure_gates_are_strict(self):
        gates = [
            ("needs_outside_step", 0.35), ("needs_decision", 0.35), ("needs_prod_investigation", 0.5),
            ("evidence_shows_done", 0.4), ("requested_thing_exists", 0.5),
        ]
        for key, edge in gates:
            a = pure_answers()
            a[key]["noul"] = edge - 0.01
            self.assertEqual(bucket(a), PURE_PR, key)
            a[key]["noul"] = edge
            self.assertNotEqual(bucket(a), PURE_PR, key)
        a = pure_answers()
        a["size"]["score"] = 2.99
        self.assertEqual(bucket(a), PURE_PR)
        a["size"]["score"] = 3.0
        self.assertEqual(bucket(a), PROBABLY_PR)
        a = pure_answers()
        a["path"]["probabilities"]["pr_only"] = 0.6
        a["needs_repo_change"]["noul"] = 0.6
        self.assertEqual(bucket(a), PURE_PR)
        a["path"]["probabilities"]["pr_only"] = 0.59
        self.assertEqual(bucket(a), PROBABLY_PR)

    def test_merged_pr_rule_needs_the_fact(self):
        a = pure_answers()
        a["evidence_shows_done"]["noul"] = 0.9
        self.assertEqual(bucket(a, {"merged_pr_count": 1}), ALREADY_RESOLVED)
        self.assertEqual(bucket(a, {"merged_pr_count": 0}), PROBABLY_PR)
        self.assertEqual(bucket(a, None), PROBABLY_PR)

    def test_version_1_answers_without_evidence_questions(self):
        a = pure_answers()
        for k in ("evidence_shows_done", "requested_thing_exists", "fix_location_known"):
            del a[k]
        self.assertEqual(bucket(a, None), PURE_PR)


class Rebucket(unittest.TestCase):
    def test_deterministic_and_order_independent(self):
        rows = example("results.example.json")
        for r in rows:
            r["bucket"] = "not_pr"  # a stale policy's output
        once = rebucket(copy.deepcopy(rows))
        twice = rebucket(copy.deepcopy(once))
        self.assertEqual(once, twice)
        self.assertEqual({r["id"]: r["bucket"] for r in once}, EXPECTED)
        shuffled = copy.deepcopy(once)
        random.Random(7).shuffle(shuffled)
        self.assertEqual(render_report(once), render_report(rebucket(shuffled)))

    def test_cli_rebucket_and_offline_eval(self):
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "out")
            os.makedirs(out)
            shutil.copy(example_path("results.example.json"), os.path.join(out, "results.json"))
            shutil.copy(example_path("results.example.json"), os.path.join(out, "eval-v2.json"))
            base = ["--out", out, "--pipeline-config", os.path.join(tmp, "none.json")]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(triage.main(["--rebucket"] + base), 0)
            first = read(os.path.join(out, "results.json"))
            report = read(os.path.join(out, "report.md"))
            with redirect_stdout(io.StringIO()):
                triage.main(["--rebucket"] + base)
            self.assertEqual(first, read(os.path.join(out, "results.json")))
            self.assertEqual(report, read(os.path.join(out, "report.md")))
            self.assertIn("pure_pr=1 likely_pr_review=4", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                triage.main(["--rebucket", "--eval", example_path("labels.example.json")] + base)
            self.assertIn("F1 75.0%", buf.getvalue())
            self.assertIn("misses (shipped a PR, not called PR): ENG-105=needs_decision", buf.getvalue())
        finally:
            shutil.rmtree(tmp)


class Questions(unittest.TestCase):
    def test_deploy_policy_comes_from_repository(self):
        repo = example("repository.example.json")
        q = triage.build_questions(repo)
        self.assertIn("Database migrations, which run automatically on deploy", q["path"]["instructions"]["definition"])
        self.assertNotIn("workers", json.dumps(q))
        bare = dict(repo, deploy_includes=[])
        self.assertIn("ships the code and nothing else", triage.build_questions(bare)["path"]["instructions"]["definition"])

    def test_no_evidence_drops_three_questions(self):
        repo = example("repository.example.json")
        full, v1 = triage.build_questions(repo, True), triage.build_questions(repo, False)
        self.assertEqual(set(full) - set(v1), {"evidence_shows_done", "requested_thing_exists", "fix_location_known"})


if __name__ == "__main__":
    unittest.main()
