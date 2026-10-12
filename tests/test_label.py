import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from helpers import FakeBackend, example, example_path

from ticket_triage import label
from ticket_triage.prepare import TextPolicy
from ticket_triage.taxonomy import Taxonomy, TaxonomyError, load_taxonomy


def tax():
    return load_taxonomy(example_path("taxonomy.example.json"))


def row(iid, labels, expected, dims, outcome, conf=None):
    return {"id": iid, "labels": labels, "expected": expected, "dimensions": dims, "outcome": outcome,
            "confidence": conf or {g: (None if g == "effort" else 0.9) for g in labels}, "route": label.route(dims)}


# Four synthetic backtest rows with hand-computed expectations.
ROWS = [
    row("ENG-201", {"type": "Bug", "effort": "S"}, {"type": "Bug", "effort": "S"},
        {"workability": 0.9, "ambiguity": 0.1, "effort": 0.5, "blast_radius": 1.0, "locatability": 0.8},
        {"pr": True, "decision": False, "human_answer": False, "cycles": 1}),
    row("ENG-202", {"type": "Enhancement", "effort": "M"}, {"type": "Enhancement", "effort": "L"},
        {"workability": 0.7, "ambiguity": 0.7, "effort": 1.5, "blast_radius": 1.0, "locatability": 0.6},
        {"pr": True, "decision": True, "human_answer": False, "cycles": 2}),
    row("ENG-203", {"type": "Question", "effort": "L"}, {"effort": "L"},
        {"workability": 0.2, "ambiguity": 0.8, "effort": 2.5, "blast_radius": 2.0, "locatability": 0.3},
        {"pr": False, "decision": True, "human_answer": True, "cycles": 0}),
    row("ENG-204", {"type": "Chore", "effort": "S"}, {"type": "Refactor"},
        {"workability": 0.3, "ambiguity": 0.2, "effort": 0.6, "blast_radius": 0.5, "locatability": 0.9},
        {"pr": False, "decision": False, "human_answer": False, "cycles": 0}),
]


class TaxonomyFile(unittest.TestCase):
    def test_example_loads(self):
        t = tax()
        self.assertIn("label_type", t.label_questions())
        self.assertNotIn("truth", json.dumps(t.dimension_questions()))

    def test_rejects_bad_files(self):
        good = example("taxonomy.example.json")
        bad = copy.deepcopy(good)
        bad["groups"]["type"]["labels"]["Bug"] = ""
        self.assertRaises(TaxonomyError, Taxonomy, bad)
        bad = copy.deepcopy(good)
        bad["historic"]["Old"] = ["type", "Nonexistent"]
        self.assertRaises(TaxonomyError, Taxonomy, bad)
        bad = copy.deepcopy(good)
        bad["effort_bands"] = [[2.0, "S"], [1.0, "M"]]
        self.assertRaises(TaxonomyError, Taxonomy, bad)
        self.assertRaises(TaxonomyError, Taxonomy, {})

    def test_historic_and_bands(self):
        t = tax()
        self.assertEqual(t.historic_expected(["Improvement", "Size: M", "Misc", "Billing", "Unknown"]),
                         {"type": "Enhancement", "effort": "M", "area": "Billing"})
        self.assertEqual([t.effort_band(s) for s in (0.0, 0.99, 1.0, 2.5, 3.9, 120)], ["S", "S", "M", "L", "XL", "XL"])


class Backtest(unittest.TestCase):
    def test_arithmetic(self):
        s = label.summarize(ROWS, tax())
        self.assertEqual(s["labels"]["type"], {"n": 3, "agree": 2, "rate": 2 / 3, "top_disagreements": [("Refactor->Chore", 1)]})
        self.assertEqual(s["labels"]["area"]["n"], 0)
        self.assertEqual((s["labels"]["effort"]["n"], s["labels"]["effort"]["agree"]), (3, 2))
        w = s["dimensions"]["workability"]
        self.assertEqual((w["tp"], w["fp"], w["fn"], w["tn"]), (2, 0, 0, 2))
        a = s["dimensions"]["ambiguity"]
        self.assertEqual((a["tp"], a["fp"], a["fn"], a["tn"]), (1, 1, 0, 2))
        self.assertAlmostEqual(a["precision"], 0.5)
        self.assertFalse(s["dimensions"]["effort"]["validated"])
        self.assertEqual(s["routes"]["ready"], {"n": 1, "pr": 1, "decision": 0, "human_answer": 0})
        self.assertEqual(s["routes"]["needs_decision"]["n"], 2)
        self.assertEqual(s["routes"]["needs_ops_step"]["n"], 1)
        text = label.format_summary(s)
        self.assertIn("(no human labels map to this group)", text)

    def test_empty_history(self):
        s = label.summarize([], tax())
        self.assertEqual(s["n"], 0)
        self.assertIsNone(s["labels"]["type"]["rate"])
        label.format_summary(s)
        c = label.compare_relabel([], [])
        self.assertIsNone(c["human_vs_shipped"]["rate"])
        res = label.calibrate_effort([], tax())
        self.assertIsNone(res["suggested"])
        self.assertIn("problem", res)

    def test_cli_with_no_outcomes_needs_no_keys(self):
        tmp = tempfile.mkdtemp()
        env = {k: os.environ.pop(k) for k in ("LINEAR_API_KEY", "TYPESAFE_API_KEY") if k in os.environ}
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = label.main(["--taxonomy", example_path("taxonomy.example.json"), "--backtest",
                                 "--artifacts", os.path.join(tmp, "missing")])
            self.assertEqual(rc, 0)
            self.assertIn("no labelled history", buf.getvalue())
            empty = os.path.join(tmp, "outcomes.json")
            with open(empty, "w") as f:
                f.write("{}")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(label.main(["--taxonomy", example_path("taxonomy.example.json"), "--backtest",
                                             "--outcomes", empty]), 0)
        finally:
            os.environ.update(env)
            shutil.rmtree(tmp)

    def test_outcomes_from_artifacts(self):
        tmp = tempfile.mkdtemp()
        try:
            for d, files in (("ENG-1", ["pr-body.md", "cycle-1/plan.md", "cycle-2-plan.md"]),
                             ("OPS-7", ["decision.md", "human-answer.md"]), ("notes", ["x.md"])):
                for f in files:
                    p = os.path.join(tmp, d, f)
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    open(p, "w").close()
            out = label.outcomes_from_artifacts(tmp)
            self.assertEqual(sorted(out), ["ENG-1", "OPS-7"])
            self.assertEqual(out["ENG-1"], {"pr": True, "decision": False, "human_answer": False, "cycles": 2})
            self.assertEqual(out["OPS-7"]["human_answer"], True)
        finally:
            shutil.rmtree(tmp)

    def test_score_all_with_fake_backend(self):
        t = tax()

        def answers(state, questions):
            out = {}
            for k, q in questions.items():
                if q["type"] == "choice":
                    first = list(q["criteria"])[0]
                    out[k] = {"choice": first, "probabilities": {first: 1.0}, "confidence": 0.7}
                elif q["type"] == "noul":
                    out[k] = {"noul": 0.8}
                else:
                    out[k] = {"score": 1.4, "confidence": 0.5}
            return out

        fake = FakeBackend(answers)
        issues = [i for i in example("issues.example.json") if i["identifier"].startswith("ENG-20")]
        rows, failures = label.score_all(issues, fake, t, TextPolicy(), workers=2)
        self.assertEqual(failures, [])
        self.assertEqual([r["id"] for r in rows], ["ENG-201", "ENG-202", "ENG-203", "ENG-204"])
        for state, _ in fake.calls:
            self.assertNotIn("labels", state["issue"])  # the human label never reaches the model
        r = rows[0]
        self.assertEqual(r["labels"], {"type": "Bug", "area": "Accounts", "effort": "M"})
        self.assertEqual(r["expected"], {"type": "Bug", "effort": "S"})
        self.assertEqual(r["route"], "needs_decision")  # ambiguity 0.8


class AuditAndCalibration(unittest.TestCase):
    def test_audit(self):
        rows = copy.deepcopy(ROWS)
        rows[3]["confidence"]["type"] = 0.5
        diffs = label.audit(rows, 0.8)
        by = {(d["id"], d["group"]): d for d in diffs}
        self.assertEqual(set(by), {("ENG-202", "effort"), ("ENG-204", "type")})
        self.assertFalse(by[("ENG-204", "type")]["apply"])
        self.assertIsNone(by[("ENG-202", "effort")]["confidence"])  # banded in code: no model confidence
        self.assertFalse(by[("ENG-202", "effort")]["apply"])

    def test_apply_is_not_wired(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "bt.json")
            with open(p, "w") as f:
                json.dump(ROWS, f)
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    label.main(["--taxonomy", example_path("taxonomy.example.json"), "--audit", p, "--apply"])
            self.assertIn("not wired", str(cm.exception.code))
        finally:
            shutil.rmtree(tmp)

    def test_calibration_holds_out_half(self):
        t = tax()
        rows = []
        centres = {"S": 0.4, "M": 1.4, "L": 2.4, "XL": 3.6}
        n = 0
        for band, c in centres.items():
            for k in range(4):
                n += 1
                rows.append({"id": "ENG-{:03d}".format(n), "dimensions": {"effort": c + 0.05 * k}, "expected": {"effort": band}})
        res = label.calibrate_effort(rows, t)
        self.assertEqual((res["n_fit"], res["n_held_out"]), (8, 8))
        self.assertEqual([b for _, b in res["suggested"]], ["S", "M", "L", "XL"])
        self.assertAlmostEqual(res["suggested"][0][0], (0.45 + 1.45) / 2, places=2)  # fit half: k = 0 and 2 of each band
        self.assertEqual(res["held_out"]["exact"], 1.0)


if __name__ == "__main__":
    unittest.main()
