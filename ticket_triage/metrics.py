"""Evaluation arithmetic. Every rate is guarded: an empty denominator gives None, never an exception."""

import math


def ratio(k, n):
    return None if not n else k / n


def wilson(k, n, z=1.96):
    """95% Wilson score interval for k successes in n trials, as (low, high) fractions, or None."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def confusion(pairs):
    """Binary confusion counts and rates from (predicted, actual) boolean pairs."""
    pairs = list(pairs)
    tp = sum(1 for p, a in pairs if p and a)
    fp = sum(1 for p, a in pairs if p and not a)
    fn = sum(1 for p, a in pairs if not p and a)
    tn = len(pairs) - tp - fp - fn
    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else None
    return {
        "n": len(pairs), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "precision_ci95": wilson(tp, tp + fp),
        "recall": recall, "recall_ci95": wilson(tp, tp + fn),
        "f1": f1,
        "accuracy": ratio(tp + tn, len(pairs)),
        "false_positive_rate": ratio(fp, fp + tn),
    }


def fmt_rate(x):
    return "n/a" if x is None else "{:.1%}".format(x)


def fmt_ci(ci):
    return "" if ci is None else " [{:.0%}-{:.0%}]".format(ci[0], ci[1])
