import unittest

from helpers import example

from ticket_triage.metrics import confusion, wilson
from ticket_triage.triage import evaluate, format_eval


def truth_of(labels):
    t = {i: True for i in labels["positive"]}
    t.update({i: False for i in labels["negative"]})
    return t


class Evaluate(unittest.TestCase):
    def setUp(self):
        self.rows = example("results.example.json")
        self.truth = truth_of(example("labels.example.json"))

    def test_counts_and_rates(self):
        ev = evaluate(self.rows, self.truth)
        self.assertEqual((ev["n"], ev["positives"], ev["negatives"]), (8, 4, 4))
        p = ev["pure_pr"]
        self.assertEqual((p["tp"], p["fp"], p["fn"], p["tn"]), (1, 0, 3, 4))
        self.assertAlmostEqual(p["precision"], 1.0)
        self.assertAlmostEqual(p["recall"], 0.25)
        self.assertAlmostEqual(p["f1"], 0.4)
        q = ev["pure_or_probably_pr"]
        self.assertEqual((q["tp"], q["fp"], q["fn"], q["tn"]), (3, 1, 1, 3))
        self.assertAlmostEqual(q["precision"], 0.75)
        self.assertAlmostEqual(q["recall"], 0.75)
        self.assertAlmostEqual(q["f1"], 0.75)
        self.assertAlmostEqual(q["false_positive_rate"], 0.25)
        self.assertEqual(ev["misses"], ["ENG-105=needs_decision"])
        self.assertEqual(ev["false_alarms"], [])
        self.assertEqual(ev["by_bucket"]["likely_pr_review"], {"positive": 2, "negative": 1})
        self.assertEqual(ev["missing"], [])

    def test_one_sided_labels_do_not_divide_by_zero(self):
        only_pos = {"ENG-105": True}
        ev = evaluate(self.rows, only_pos)
        self.assertIsNone(ev["pure_pr"]["precision"])
        self.assertIsNone(ev["pure_pr"]["false_positive_rate"])
        self.assertIsNone(ev["pure_pr"]["f1"])
        self.assertIn("n/a", format_eval(ev))
        ev = evaluate(self.rows, {"ENG-106": False, "ENG-999": True})
        self.assertIsNone(ev["pure_pr"]["recall"])
        self.assertEqual(ev["missing"], ["ENG-999"])
        format_eval(ev)

    def test_empty(self):
        ev = evaluate([], {})
        self.assertEqual(ev["n"], 0)
        self.assertIsNone(ev["pure_pr"]["accuracy"])
        format_eval(ev)


class Wilson(unittest.TestCase):
    def test_known_values(self):
        self.assertIsNone(wilson(0, 0))
        lo, hi = wilson(14, 22)
        self.assertAlmostEqual(lo, 0.430, places=2)
        self.assertAlmostEqual(hi, 0.803, places=2)
        lo, hi = wilson(0, 5)
        self.assertEqual(lo, 0.0)

    def test_confusion_from_pairs(self):
        c = confusion([(True, True), (True, False), (False, True), (False, False)])
        self.assertEqual((c["tp"], c["fp"], c["fn"], c["tn"], c["accuracy"]), (1, 1, 1, 1, 0.5))


if __name__ == "__main__":
    unittest.main()
