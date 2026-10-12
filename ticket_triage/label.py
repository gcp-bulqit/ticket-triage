"""Infer tracker labels and scoring dimensions for an issue, from a taxonomy you supply.

Labels are inferred from the ticket text against the taxonomy's groups, independently of whatever
labels the ticket already carries. The human label is never an input; it is only ever compared
against, which is what makes the audit meaningful.

Scores are kept per dimension, never collapsed into one number here. A routing rule reads them in
`route()`, which can change without re-running anything.

Modes:
  --ids ENG-1,ENG-2       score named issues and write results
  --backtest              score issues whose outcome is known and report label agreement against
                          the human labels and dimension accuracy against what actually happened
  --relabel               classify what each issue turned out to be, from its merged PR description
                          (sends that description to the model); --against compares with a backtest
  --audit FILE            list disagreements between inferred and human labels in a backtest file,
                          marking those at or above --threshold. Offline.
  --calibrate-effort FILE fit effort band edges on half of a backtest file and test them on the
                          other half. Offline.

--apply is not wired to tracker writes. It exits after the audit report, on purpose: review it first.

Outcomes come from --outcomes outcomes.json ({"ENG-1": {"pr": true, "decision": false,
"human_answer": false, "cycles": 1}, ...}) or, for the linear-pipeline, from its artifact folders
(--artifacts .issues: pr-body.md = produced a PR, decision.md = a decision brief was written,
human-answer.md = a person had to answer, cycle-*plan.md = review cycles).

Needs LINEAR_API_KEY and TYPESAFE_API_KEY for the modes that fetch or score. Ticket text goes to
TypeSafe and to the output files; it is never printed.

Usage:
  python -m ticket_triage.label --taxonomy taxonomy.json --ids ENG-101,ENG-102
  python -m ticket_triage.label --taxonomy taxonomy.json --backtest --outcomes outcomes.json
  python -m ticket_triage.label --taxonomy taxonomy.json --audit .agent/ticket-label/backtest.json
  python -m ticket_triage.label --taxonomy taxonomy.json --calibrate-effort .agent/ticket-label/backtest.json
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys

from . import linear
from .jev import DEFAULT_MODEL, get_backend
from .metrics import confusion, fmt_rate, ratio
from .prepare import TextPolicy, issue_state, visible_comments
from .taxonomy import TaxonomyError, load_taxonomy

PR_BODY_LIMIT = 6000
OUTCOME_KEYS = ("pr", "decision", "human_answer", "cycles")

# --------------------------------------------------------------------------- scoring


def build_state(issue, comments, policy):
    """Deliberately omits `labels`. The human label must not reach the model."""
    return {"issue": issue_state(issue, comments, policy, include_labels=False)}


def route(d):
    """Code-owned policy over the dimensions. Thresholds are starting points, not truths.

    Needs `workability` and `ambiguity`; a taxonomy without them is "unrouted".
    """
    if "workability" not in d or "ambiguity" not in d:
        return "unrouted"
    work, amb = d["workability"], d["ambiguity"]
    blast = d.get("blast_radius", 0.0)
    loc = d.get("locatability", 0.0)
    if amb >= 0.6:
        return "needs_decision"
    if work < 0.4:
        return "needs_ops_step"
    if blast >= 3.0:
        return "needs_care"          # workable, but wrong is expensive: review before autonomy
    if work >= 0.6 and amb < 0.35 and loc >= 0.5:
        return "ready"
    return "needs_grooming"


def interpret(answers, tax):
    """Turn raw answers into labels, per-group confidence, dimensions and a route."""
    labels, conf = {}, {}
    for g in tax.groups:
        a = answers.get("label_" + g)
        if a:
            labels[g] = a["choice"]
            conf[g] = a.get("confidence", 0.0)
    dims = {}
    for d in tax.dimensions:
        if d in answers:
            dims[d] = answers[d].get("noul", answers[d].get("score"))
    # The model gives a magnitude; the band is binned from it in code, which beat asking the model
    # to choose a band directly.
    if "effort" in dims and tax.effort_bands:
        labels["effort"] = tax.effort_band(dims["effort"])
        conf["effort"] = None
    return labels, conf, dims, route(dims)


def score(issue, backend, tax, policy):
    comments = visible_comments(issue, policy)
    questions = dict(tax.label_questions())
    questions.update(tax.dimension_questions())
    res = backend.ask(build_state(issue, comments, policy), questions)
    labels, conf, dims, rt = interpret(res["answers"], tax)
    return {"id": issue["identifier"], "title": issue["title"], "labels": labels, "confidence": conf,
            "dimensions": dims, "route": rt, "model": res["model"], "usage": res.get("usage"),
            "answers": res["answers"]}


def score_all(issues, backend, tax, policy, workers=8):
    rows, failures = [], []
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(score, i, backend, tax, policy): i for i in issues}
        for f in cf.as_completed(futs):
            issue = futs[f]
            try:
                r = f.result()
            except Exception as e:
                failures.append((issue["identifier"], str(e)[:120]))
                continue
            r["human_labels"] = sorted(l["name"] for l in ((issue.get("labels") or {}).get("nodes") or []))
            r["expected"] = tax.historic_expected(r["human_labels"])
            rows.append(r)
    rows.sort(key=lambda r: r["id"])
    return rows, sorted(failures)


# --------------------------------------------------------------------------- outcomes


def outcomes_from_artifacts(artifacts_dir, issue_key=linear.DEFAULT_ISSUE_KEY):
    """What actually happened, from the linear-pipeline's per-issue artifact folders."""
    out = {}
    if not os.path.isdir(artifacts_dir):
        return out
    key = re.compile(issue_key)
    for d in sorted(os.listdir(artifacts_dir)):
        p = os.path.join(artifacts_dir, d)
        if not os.path.isdir(p) or not key.fullmatch(d):
            continue
        files = set()
        for dp, _, fs in os.walk(p):
            for f in fs:
                files.add(os.path.relpath(os.path.join(dp, f), p))
        out[d] = {
            "pr": "pr-body.md" in files,
            "decision": "decision.md" in files,
            "human_answer": "human-answer.md" in files,
            "cycles": len([f for f in files if f.startswith("cycle-") and f.endswith("plan.md")]),
        }
    return out


def load_outcomes(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {i: {k: v for k, v in o.items() if k in OUTCOME_KEYS} for i, o in data.items()}


# --------------------------------------------------------------------------- backtest


def label_agreement(rows, tax):
    """Per group: how often the inferred label matches the human label, where one exists."""
    out = {}
    groups = list(tax.groups) + (["effort"] if tax.effort_bands else [])
    for g in groups:
        pairs = [(r["expected"][g], r["labels"].get(g)) for r in rows if g in (r.get("expected") or {})]
        agree = sum(a == b for a, b in pairs)
        wrong = {}
        for a, b in pairs:
            if a != b:
                k = "{}->{}".format(a, b)
                wrong[k] = wrong.get(k, 0) + 1
        out[g] = {"n": len(pairs), "agree": agree, "rate": ratio(agree, len(pairs)),
                  "top_disagreements": sorted(wrong.items(), key=lambda kv: (-kv[1], kv[0]))[:3]}
    return out


def dimension_accuracy(rows, tax):
    """Each dimension against the outcome its `truth` names. No truth: reported, not validated."""
    out = {}
    for dim, spec in tax.dimensions.items():
        truth = spec.get("truth")
        vals = [(r["dimensions"][dim], (r.get("outcome") or {})[truth]) for r in rows
                if dim in (r.get("dimensions") or {}) and truth and truth in (r.get("outcome") or {})]
        if not truth or not vals:
            out[dim] = {"truth": truth, "n": 0, "validated": False}
        elif spec["type"] == "noul":
            out[dim] = dict(confusion((p >= 0.5, bool(t)) for p, t in vals), truth=truth, validated=True)
        else:
            by = {}
            for p, t in vals:
                by.setdefault(int(t), []).append(p)
            out[dim] = {"truth": truth, "n": len(vals), "validated": True,
                        "mean_by_actual": {k: (sum(v) / len(v), len(v)) for k, v in sorted(by.items())}}
    return out


def route_outcomes(rows):
    out = {}
    for r in rows:
        o = r.get("outcome") or {}
        s = out.setdefault(r["route"], {"n": 0, "pr": 0, "decision": 0, "human_answer": 0})
        s["n"] += 1
        for k in ("pr", "decision", "human_answer"):
            s[k] += bool(o.get(k))
    return out


def summarize(rows, tax):
    return {"n": len(rows), "labels": label_agreement(rows, tax),
            "dimensions": dimension_accuracy(rows, tax), "routes": route_outcomes(rows)}


def format_summary(s, failures=()):
    lines = ["backtest over {} issues".format(s["n"]) + ("  ({} failed)".format(len(failures)) if failures else "")]
    lines += ["   FAILED {}: {}".format(i, e) for i, e in list(failures)[:5]]
    lines += ["", "--- label agreement with the human label, where one exists ---",
              "{:14} {:>4} {:>6} {:>7}   top disagreements".format("group", "n", "agree", "rate")]
    for g, a in s["labels"].items():
        if not a["n"]:
            lines.append("{:14} {:>4} {:>6} {:>7}   (no human labels map to this group)".format(g, 0, "-", "-"))
            continue
        top = ", ".join("{} x{}".format(k, v) for k, v in a["top_disagreements"])
        lines.append("{:14} {:>4} {:>6} {:>7}   {}".format(g, a["n"], a["agree"], fmt_rate(a["rate"]), top))
    lines += ["", "--- dimension accuracy against what actually happened ---"]
    for dim, d in s["dimensions"].items():
        if not d["validated"]:
            lines.append("{:14} no ground truth available; reported but unvalidated".format(dim))
        elif "mean_by_actual" in d:
            parts = "  ".join("{}:{:.2f}(n={})".format(k, m, n) for k, (m, n) in d["mean_by_actual"].items())
            lines.append("{:14} n={:<4} mean predicted by actual {}:  {}".format(dim, d["n"], d["truth"], parts))
        else:
            lines.append("{:14} n={:<4} accuracy {}   precision {} ({}/{})   recall {} ({}/{})   (vs {})".format(
                dim, d["n"], fmt_rate(d["accuracy"]), fmt_rate(d["precision"]), d["tp"], d["tp"] + d["fp"],
                fmt_rate(d["recall"]), d["tp"], d["tp"] + d["fn"], d["truth"]))
    lines += ["", "--- route distribution ---"]
    for k, v in sorted(s["routes"].items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        lines.append("   {:16} {:4}   produced a PR {}/{}   needed a decision {}/{}   a person answered {}/{}".format(
            k, v["n"], v["pr"], v["n"], v["decision"], v["n"], v["human_answer"], v["n"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- relabel


def shipped_type(iid, body, backend, tax, group="type"):
    """Classify what an issue turned out to be, from the pull request that resolved it.

    The ticket text is what the humans labelled from, so it cannot arbitrate its own label. The
    merged PR's description says what the change actually did, independently of how the ticket was
    worded and of what anyone labelled it. That makes it a basis for the taxonomy rather than another
    opinion about it. Note: this sends the PR description to the model.
    """
    q = {"shipped": {
        "type": "choice",
        "instructions": {
            "how_to_read": "`shipped` is the description of a pull request that has already been merged. It says what the change did.",
            "question": "Judging only by what this change actually did, what kind of change was it?",
        },
        "criteria": dict(tax.groups[group]["labels"]),
    }}
    a = backend.ask({"shipped": {"body": body[:PR_BODY_LIMIT]}}, q)["answers"]["shipped"]
    return {"id": iid, "shipped": a["choice"], "confidence": a.get("confidence", 0.0),
            "probabilities": a.get("probabilities", {})}


def compare_relabel(rows, backtest_rows, group="type"):
    """Human label vs what shipped, and model-from-ticket-text vs what shipped."""
    by = {r["id"]: r for r in rows}
    text = {r["id"]: r for r in backtest_rows}
    human = [((text[i].get("expected") or {})[group], by[i]["shipped"]) for i in by
             if i in text and group in (text[i].get("expected") or {})]
    model = [(text[i]["labels"][group], by[i]["shipped"]) for i in by if i in text and text[i]["labels"].get(group)]
    def stat(pairs):
        agree = sum(a == b for a, b in pairs)
        return {"n": len(pairs), "agree": agree, "rate": ratio(agree, len(pairs))}
    return {"human_vs_shipped": stat(human), "model_vs_shipped": stat(model)}


# --------------------------------------------------------------------------- audit and calibration


def audit(rows, threshold):
    """Disagreements between the inferred label and the one on the ticket."""
    out = []
    for r in rows:
        for g, expected in sorted((r.get("expected") or {}).items()):
            got = r["labels"].get(g)
            conf = r["confidence"].get(g)
            if got and got != expected:
                out.append({"id": r["id"], "group": g, "current": expected, "suggested": got,
                            "confidence": conf, "apply": conf is not None and conf >= threshold})
    return out


def calibrate_effort(rows, tax):
    """Fit effort band edges on half the labelled rows and test them on the other half.

    Edges are midpoints between the mean effort score each human band drew. Fitting and reporting on
    the same rows would be memorising the test set, so rows are split deterministically by id
    (alternate rows after sorting) and accuracy is reported on the held-out half only.
    """
    names = [n for _, n in tax.effort_bands]
    pts = sorted((r["id"], r["dimensions"]["effort"], r["expected"]["effort"]) for r in rows
                 if "effort" in (r.get("dimensions") or {}) and (r.get("expected") or {}).get("effort") in names)
    fit, held = pts[0::2], pts[1::2]
    means = {}
    for _, s, band in fit:
        means.setdefault(band, []).append(s)
    result = {"n_fit": len(fit), "n_held_out": len(held),
              "means": {b: (sum(v) / len(v), len(v)) for b, v in means.items()}, "suggested": None}
    def accuracy(edges):
        def band_of(s):
            for e, n in edges:
                if s < e:
                    return n
            return edges[-1][1]
        pairs = [(names.index(b), names.index(band_of(s))) for _, s, b in held]
        return {"exact": ratio(sum(a == b for a, b in pairs), len(pairs)),
                "within_one": ratio(sum(abs(a - b) <= 1 for a, b in pairs), len(pairs))}
    if names:
        result["current"] = accuracy(tax.effort_bands)
    if len(names) < 2 or any(n not in means for n in names):
        result["problem"] = "every band needs at least one labelled row in the fitting half"
        return result
    m = [result["means"][n][0] for n in names]
    if m != sorted(m):
        result["problem"] = "band means are not increasing; the effort question does not order these bands"
        return result
    edges = [((m[i] + m[i + 1]) / 2, names[i]) for i in range(len(names) - 1)] + [(99.0, names[-1])]
    result["suggested"] = [[round(e, 2), n] for e, n in edges]
    result["held_out"] = accuracy(edges)
    return result


# --------------------------------------------------------------------------- CLI


def require_env(*keys):
    for k in keys:
        if not os.environ.get(k):
            sys.exit("ticket_label: {} is not set".format(k))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ticket_triage.label",
                                 description="Infer labels and scoring dimensions from a taxonomy you supply.")
    ap.add_argument("--taxonomy", required=True, help="taxonomy JSON (see examples/taxonomy.example.json)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ids", help="comma-separated issue ids to score")
    mode.add_argument("--backtest", action="store_true", help="score issues with known outcomes and report")
    mode.add_argument("--relabel", action="store_true", help="classify what each issue turned out to be, from its merged PR")
    mode.add_argument("--audit", metavar="BACKTEST", help="report label disagreements in a backtest file (offline)")
    mode.add_argument("--calibrate-effort", metavar="BACKTEST", help="fit effort band edges on held-out halves (offline)")
    ap.add_argument("--outcomes", help="outcomes JSON: {id: {pr, decision, human_answer, cycles}}")
    ap.add_argument("--artifacts", default=".issues", help="linear-pipeline artifact folders, used when --outcomes is absent")
    ap.add_argument("--issue-key", default=linear.DEFAULT_ISSUE_KEY, help="regex an issue id must match (default: %(default)s)")
    ap.add_argument("--against", help="with --relabel: a backtest file to compare the shipped verdict against")
    ap.add_argument("--group", default="type", help="with --relabel: the label group to judge from the PR (default: type)")
    ap.add_argument("--apply", action="store_true", help="with --audit: NOT WIRED; exits after the report")
    ap.add_argument("--threshold", type=float, default=0.8, help="with --audit: confidence at which a correction is marked")
    ap.add_argument("--limit", type=int, default=0, help="cap the backtest size")
    ap.add_argument("--ignore-comment", action="append", default=[], help="regex; matching comments are dropped; repeatable")
    ap.add_argument("--cut-description", action="append", default=[], help="regex; description is cut at its first match")
    ap.add_argument("--include-comment-authors", action="store_true", help="send comment authors' names (default: pseudonyms)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(".agent", "ticket-label", "backtest.json"))
    a = ap.parse_args(argv)

    try:
        tax = load_taxonomy(a.taxonomy)
    except (OSError, ValueError, TaxonomyError) as e:
        sys.exit("ticket_label: taxonomy {}: {}".format(a.taxonomy, e))
    policy = TextPolicy([re.compile(p, re.I) for p in a.ignore_comment],
                        [re.compile(p, re.I) for p in a.cut_description], a.include_comment_authors)

    if a.audit:
        diffs = audit(read_json(a.audit), a.threshold)
        print("{} disagreements; {} at or above threshold {}".format(len(diffs), sum(d["apply"] for d in diffs), a.threshold))
        for d in sorted(diffs, key=lambda x: -(x["confidence"] or 0))[:40]:
            print("  {} {:10} {:9} {:18} -> {:18} conf {}".format(
                "APPLY" if d["apply"] else "     ", d["id"], d["group"], d["current"], d["suggested"],
                "-" if d["confidence"] is None else "{:.2f}".format(d["confidence"])))
        if a.apply:
            sys.exit("ticket_label: --apply is not wired to tracker writes; review the report first")
        return 0

    if a.calibrate_effort:
        if not tax.effort_bands:
            sys.exit("ticket_label: the taxonomy has no effort_bands to calibrate")
        res = calibrate_effort(read_json(a.calibrate_effort), tax)
        print(json.dumps(res, indent=1, sort_keys=True))
        return 0

    if a.backtest or a.relabel:
        outcomes = load_outcomes(a.outcomes) if a.outcomes else outcomes_from_artifacts(a.artifacts, a.issue_key)
        if not outcomes:
            print("no labelled history: no outcomes in {}; nothing to {}".format(
                a.outcomes or a.artifacts, "backtest" if a.backtest else "relabel"))
            return 0

    if a.relabel:
        if a.group not in tax.groups:
            sys.exit("ticket_label: the taxonomy has no group {!r}".format(a.group))
        require_env("TYPESAFE_API_KEY")
        bodies = {}
        for iid in sorted(outcomes):
            p = os.path.join(a.artifacts, iid, "pr-body.md")
            if os.path.exists(p):
                with open(p, encoding="utf-8", errors="ignore") as f:
                    bodies[iid] = f.read()
        if not bodies:
            print("no merged PR descriptions under {}; nothing to relabel".format(a.artifacts))
            return 0
        backend = get_backend("jev", a.model)
        rows = []
        with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            futs = [ex.submit(shipped_type, i, b, backend, tax, a.group) for i, b in sorted(bodies.items())]
            for f in futs:
                try:
                    rows.append(f.result())
                except Exception:
                    pass
        write_json(a.out, rows)
        dist = {}
        for r in rows:
            dist[r["shipped"]] = dist.get(r["shipped"], 0) + 1
        print("what {} issues turned out to be, judged from the merged PR:".format(len(rows)))
        for k, v in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0])):
            print("   {:14} {:4}".format(k, v))
        if a.against:
            c = compare_relabel(rows, read_json(a.against), a.group)
            for name, s in c.items():
                print("{:18} n={}  agree {}  rate {}".format(name, s["n"], s["agree"], fmt_rate(s["rate"])))
        return 0

    require_env("LINEAR_API_KEY", "TYPESAFE_API_KEY")
    backend = get_backend("jev", a.model)
    if a.backtest:
        ids = sorted(outcomes)[: a.limit] if a.limit else sorted(outcomes)
        issues = linear.fetch_by_ids(ids, a.issue_key)
        rows, failures = score_all(issues, backend, tax, policy, a.workers)
        for r in rows:
            r["outcome"] = outcomes.get(r["id"], {})
        write_json(a.out, rows)
        print(format_summary(summarize(rows, tax), failures))
        return 0

    issues = linear.fetch_by_ids(a.ids.split(","), a.issue_key)
    rows, failures = score_all(issues, backend, tax, policy, a.workers)
    write_json(a.out, rows)
    for r in rows:
        print("{:10} {:16} {}  {}".format(r["id"], r["route"], r["labels"],
                                         {k: round(v, 2) for k, v in r["dimensions"].items()}))
    for i, e in failures:
        print("FAILED {}: {}".format(i, e), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
