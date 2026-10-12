"""Which tracker issues can be resolved purely by a pull request?

Fetches issues from Linear (by assignee and workflow states, or by id), gathers code evidence from
the local repository, asks the model a fixed set of typed questions per issue in one call, and
decides the bucket in code. Ticket text goes from Linear to the model and to the output files; it
is never printed.

Usage (from the target repository's root):
  python -m ticket_triage.triage --repository repository.json --assignee "Pipeline Bot" [--states Todo,Backlog]
  python -m ticket_triage.triage --repository repository.json --ids ENG-101,ENG-102
  python -m ticket_triage.triage --repository repository.json --eval labels.json [--no-evidence]
  python -m ticket_triage.triage --rebucket [--emit-triage] [--emit-exclude-likely]
  python -m ticket_triage.triage --rebucket --eval labels.json      re-score a stored eval, offline

Defaults come from .agent/config.json when it exists (the linear-pipeline's config): --assignee
from linear.user, --base from git.baseBranch, --ignore-comment from linear.unparkIgnorePatterns.
Arguments override them.

The bucket policy lives in `bucket()` and reads only stored answers. Re-run with --rebucket to apply
a changed policy to results.json without calling the model again. Thresholds are starting points,
not truths.

Outputs, in --out (default .agent/ticket-triage; it holds ticket text, keep it out of git):
  results.json        raw answers, evidence and the code-derived bucket, per issue
  report.md           one table row per issue, failures listed first
  eval-v1/v2.json     with --eval: the same, plus the label
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time

from . import linear
from .evidence import CodeEvidence
from .jev import DEFAULT_MODEL, get_backend
from .metrics import confusion, fmt_ci, fmt_rate
from .prepare import TextPolicy, issue_state, visible_comments

# Bucket names are stored in results files; they are kept exactly as the v2 policy wrote them.
PURE_PR = "pure_pr"
PROBABLY_PR = "likely_pr_review"
PR_PLUS_OPS = "pr_plus_ops"
NEEDS_DECISION = "needs_decision"
NOT_PR = "not_pr"
ALREADY_RESOLVED = "already_resolved"
BUCKETS = [PURE_PR, PROBABLY_PR, PR_PLUS_OPS, NEEDS_DECISION, NOT_PR, ALREADY_RESOLVED]

REPOSITORY_KEYS = ("name", "contains", "does_not_contain")

# --------------------------------------------------------------------------- questions


def deploy_sentence(repo):
    """What a routine deploy does on its own, from repository.json's `deploy_includes`.

    This is a policy choice, not a fact about software in general: whether a migration, a worker
    redeploy or a cache purge "comes with the deploy" depends on your release process. It used to
    be hard-coded (a routine redeploy of background workers counted as part of every deploy), which
    was wrong for a repository whose workers deploy separately. Now it is whatever you write.
    """
    inc = [s.strip().rstrip(".") for s in repo.get("deploy_includes") or [] if s.strip()]
    if inc:
        return "A routine deploy of merged code includes: " + "; ".join(inc) + ". Those do not count as manual steps."
    return ("A routine deploy of merged code ships the code and nothing else; any other step, including one "
            "usually done around a release, counts as a step outside `repository`.")


def pure_pr_definition(repo):
    return (
        "A ticket is resolved purely by a pull request when merging changes to `repository` and deploying "
        "them is all that is needed. " + deploy_sentence(repo) + " It is NOT purely a pull request if any "
        "person must also do something outside `repository`: change a third-party dashboard setting, set an "
        "environment variable, correct production data by hand, edit content managed outside the repository "
        "(CMS pages, copy stored in a database), or act on an external account."
    )


def build_questions(repo, with_evidence=True):
    """The typed questions, one call per issue.

    The path question and the flags ask overlapping things on purpose: the flags are independent
    checks on the path answer, and the pure-PR policy requires them to agree.
    """
    definition = pure_pr_definition(repo)
    q = {
        "path": {
            "type": "choice",
            "instructions": {
                "repository": "`repository` describes what can and cannot be changed by a pull request.",
                "definition": definition,
                "question": "Read `issue` (its title, description and comments). What does resolving this ticket, as it currently stands, require?",
            },
            "criteria": {
                "pr_only": "Only code, schema, test or documentation changes in `repository`. Nothing must be done outside it, and nothing is waiting on a decision.",
                "pr_plus_ops": "Changes in `repository` AND at least one manual step outside it, such as a dashboard setting, an environment variable, a manual data fix, or an edit to content managed outside the repository.",
                "ops_only": "No change to `repository`. It is resolved entirely outside it: a dashboard or account setting, a data correction, a content edit, or a business task.",
                "decision_first": "A product, business or design decision must be made before anyone can say what to build. The ticket asks for something whose requirements are still open.",
                "question_or_info": "A question, notice, FYI, discussion, research or investigation request whose outcome is an answer or a report, not a change.",
                "already_resolved": "The comments show it was already fixed, shipped, declined, or is a duplicate of another ticket.",
            },
        },
        "needs_repo_change": {
            "type": "noul",
            "instructions": "Does resolving `issue` require changing code, database schema, tests or documentation inside `repository`?",
            "criteria": {
                "true": "At least one change inside `repository` is required.",
                "false": "Nothing inside `repository` needs to change.",
            },
        },
        "needs_outside_step": {
            "type": "noul",
            "instructions": {
                "repository": "`repository` lists what is outside the repository under `does_not_contain`.",
                "deploy": deploy_sentence(repo),
                "question": "Does resolving `issue` require a person to do at least one thing outside `repository`, such as changing a third-party dashboard setting, setting an environment variable, correcting production data by hand, editing content managed outside the repository, or acting on an external account?",
            },
            "criteria": {
                "true": "At least one manual step outside `repository` is required. A routine deploy of merged code does not count.",
                "false": "Merging and deploying code is enough; no manual step outside `repository` is required.",
            },
        },
        "needs_decision": {
            "type": "noul",
            "instructions": "Does `issue` leave open a product, business or design question that must be answered before implementation can start? Ordinary implementation details an engineer would decide do not count.",
            "criteria": {
                "true": "An open product, business or design question blocks implementation.",
                "false": "What to build is clear enough to start; any remaining choices are engineering details.",
            },
        },
        "needs_prod_investigation": {
            "type": "noul",
            "instructions": "Before the fix for `issue` can be designed, must someone inspect production data, logs, or a recorded user session to find out what actually happened?",
            "criteria": {
                "true": "The cause cannot be known from the ticket and the code alone; production evidence is needed first.",
                "false": "The ticket and the code are enough to design the change.",
            },
        },
        "already_resolved": {
            "type": "noul",
            "instructions": "Do the comments on `issue` say it is already fixed, shipped, declined, closed, or a duplicate of another ticket?",
            "criteria": {
                "true": "A comment says it is fixed, shipped, declined, closed or a duplicate.",
                "false": "No comment says that.",
            },
        },
        "size": {
            "type": "score",
            "instructions": "If `issue` were implemented in `repository`, how large would the change be?",
            "criteria": [
                "Tiny: a copy, label or one-line change",
                "Small: a contained change to a few files in one area",
                "Medium: a feature or fix spanning one area, with tests",
                "Large: several areas, or schema plus UI plus background work",
                "Epic: too big for one pull request; it should be split into several tickets",
            ],
        },
    }
    if with_evidence:
        q["evidence_shows_done"] = {
            "type": "noul",
            "instructions": {
                "how_to_read": "`code_evidence` was computed from `repository` by code. `pull_requests` and `commits` name this ticket's id; `referenced_files` and `referenced_symbols` say whether names the ticket mentions exist on the base branch.",
                "question": "Does `code_evidence` show that the change `issue` asks for has already been delivered, through a merged pull request or a commit on the base branch that names this ticket?",
            },
            "criteria": {
                "true": "A merged pull request or a base-branch commit names this ticket and appears to deliver what it asks.",
                "false": "No merged pull request or commit names this ticket, or the ones that do are unmerged, closed without merging, or only partial.",
            },
        }
        q["requested_thing_exists"] = {
            "type": "noul",
            "instructions": {
                "how_to_read": "`code_evidence.referenced_files` and `code_evidence.referenced_symbols` list names from the ticket and whether they exist in `repository` today.",
                "question": "`issue` asks to add, create or build something new. Do the existing files or symbols in `code_evidence` show that the thing it asks for already exists in `repository`, possibly delivered under a different ticket? If `issue` is a bug report about behaviour of existing code, or asks to change existing code rather than add something, answer no.",
            },
            "criteria": {
                "true": "The ticket requests something new, and an existing file or symbol with that purpose is already in `repository`.",
                "false": "The ticket is a bug report or a change to existing code, or nothing in `code_evidence` shows the requested thing already exists.",
            },
        }
        q["fix_location_known"] = {
            "type": "noul",
            "instructions": "Using `issue` and `code_evidence.referenced_files` and `code_evidence.referenced_symbols`, is it known where in `repository` the change would be made? A file or symbol the ticket names that exists counts; a name that does not exist does not.",
            "criteria": {
                "true": "The ticket points at files or symbols that exist, or names a specific screen or flow precisely enough to find.",
                "false": "Nothing in the ticket locates the change, or the names it gives do not exist in `repository`.",
            },
        }
    return q


# --------------------------------------------------------------------------- policy


def bucket(a, ev=None):
    """Code-owned policy. `a` is one issue's answers. Thresholds are starting points, not truths.

    Two properties worth knowing before you tune it. The probably-PR test does not require the path
    answer to be pr_only, so a ticket whose path says decision_first but whose flags lean PR lands
    in likely_pr_review, ahead of the decision check. And the merged-PR rule needs a fact (a merged
    PR names the ticket) as well as the model's reading of it.
    """
    ev = ev or {}
    p = a["path"]["probabilities"]
    choice = a["path"]["choice"]
    outside = a["needs_outside_step"]["noul"]
    decision = a["needs_decision"]["noul"]
    resolved = a["already_resolved"]["noul"]
    repo = a["needs_repo_change"]["noul"]
    prod = a["needs_prod_investigation"]["noul"]
    done = a.get("evidence_shows_done", {}).get("noul", 0.0)
    size = a["size"]["score"]
    if resolved >= 0.6 or choice == "already_resolved":
        return ALREADY_RESOLVED
    # Code evidence outranks the ticket text: a merged PR naming the ticket is a fact, not a reading.
    if done >= 0.6 and ev.get("merged_pr_count", 0) > 0:
        return ALREADY_RESOLVED
    # Pure PR: the path answer and every independent flag agree, and nothing has to be looked up in
    # production first.
    if (
        choice == "pr_only"
        and p.get("pr_only", 0.0) >= 0.6
        and repo >= 0.6
        and outside < 0.35
        and decision < 0.35
        and prod < 0.5
        and size < 3.0
        and done < 0.4
        and a.get("requested_thing_exists", {}).get("noul", 0.0) < 0.5
    ):
        return PURE_PR
    if p.get("pr_only", 0.0) >= 0.3 and repo >= 0.5 and outside < 0.5 and decision < 0.5:
        return PROBABLY_PR
    # Otherwise the path answer leads, and the flags only break ties.
    if choice in ("ops_only", "question_or_info"):
        return NOT_PR
    if choice == "decision_first" or decision >= 0.5:
        return NEEDS_DECISION
    if choice == "pr_plus_ops" or (repo >= 0.5 and outside >= 0.5):
        return PR_PLUS_OPS
    return NOT_PR


def rebucket(results):
    """Re-apply bucket() to stored rows in place. No model call. Returns the rows."""
    for r in results:
        r["bucket"] = bucket(r["answers"], r.get("evidence"))
    return results


# --------------------------------------------------------------------------- classify


def build_state(issue, comments, evidence, repo, policy, include_labels=False):
    state = {
        "repository": {k: repo[k] for k in REPOSITORY_KEYS},
        "issue": issue_state(issue, comments, policy, include_labels=include_labels),
    }
    if evidence is not None:
        state["code_evidence"] = evidence
    return state


def classify(issue, backend, repo, policy, gatherer=None, with_evidence=True, as_of_pickup=False,
             include_labels=False):
    before = issue.get("startedAt") if as_of_pickup else None
    comments = visible_comments(issue, policy, before)
    # In an eval, links from the issue id to PRs and commits would leak the label, so only the
    # file and symbol lookups are kept.
    evidence = None
    if with_evidence:
        evidence = gatherer.gather(issue, comments, use_issue_links=not as_of_pickup) if gatherer else {}
    res = backend.ask(build_state(issue, comments, evidence, repo, policy, include_labels),
                      build_questions(repo, with_evidence))
    return {
        "id": issue["identifier"],
        "title": issue["title"],
        "state": (issue.get("state") or {}).get("name"),
        "priority": issue.get("priority"),
        "model": res["model"],
        "usage": res.get("usage"),
        "answers": res["answers"],
        "evidence": evidence,
    }


def classify_all(issues, workers=8, **kw):
    """Classify in parallel. One failure does not stop the run; failures are returned, not raised."""
    results, failures = [], []
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(classify, i, **kw): i["identifier"] for i in issues}
        for fut in cf.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:  # keep going; report failures at the end
                failures.append("{}: {}".format(futures[fut], e))
    results.sort(key=lambda r: r["id"])
    return rebucket(results), sorted(failures)


# --------------------------------------------------------------------------- outputs


def sort_rows(results):
    return sorted(results, key=lambda r: (BUCKETS.index(r["bucket"]),
                                          -r["answers"]["path"]["probabilities"].get("pr_only", 0.0), r["id"]))


def bucket_counts(results):
    return {b: sum(r["bucket"] == b for r in results) for b in BUCKETS}


def render_report(results, failures=(), model=DEFAULT_MODEL):
    results = sort_rows(results)
    counts = bucket_counts(results)
    lines = [
        "# Ticket triage: which issues can be done purely with a PR",
        "",
        "Model: {}. Issues: {}.".format(results[0]["model"] if results else model, len(results)),
        "",
        "| Bucket | Count |",
        "|---|---|",
    ] + ["| {} | {} |".format(b, counts[b]) for b in BUCKETS] + [
        "",
        "Flags: R = needs repo change, O = needs a step outside the repo, D = needs a decision, "
        "P = needs production investigation, X = already resolved, E = code evidence shows it delivered, "
        "S = the requested thing already seems to exist in the code. Shown when the probability is 0.5 or more.",
        "",
    ]
    if failures:
        lines += ["**{} issue(s) failed and are missing from this report:** ".format(len(failures)) + "; ".join(failures), ""]
    lines += ["| Issue | State | Bucket | Path (p) | p(pr_only) | Flags | Size | Title |",
              "|---|---|---|---|---|---|---|---|"]
    for r in results:
        a = r["answers"]
        flags = "".join(
            k for k, q in (("R", "needs_repo_change"), ("O", "needs_outside_step"), ("D", "needs_decision"),
                           ("P", "needs_prod_investigation"), ("X", "already_resolved"),
                           ("E", "evidence_shows_done"), ("S", "requested_thing_exists"))
            if q in a and a[q]["noul"] >= 0.5
        ) or "-"
        path = a["path"]
        title = (r.get("title") or "").replace("|", "/")[:70]
        lines.append("| {} | {} | {} | {} ({:.2f}) | {:.2f} | {} | {:.1f} | {} |".format(
            r["id"], r.get("state"), r["bucket"], path["choice"], path["probabilities"].get(path["choice"], 0.0),
            path["probabilities"].get("pr_only", 0.0), flags, a["size"]["score"], title))
    return "\n".join(lines) + "\n"


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- eval


def load_labels(path):
    """{"positive": [ids that shipped as a PR], "negative": [ids closed with no code]} -> {id: bool}."""
    labels = read_json(path)
    pos, neg = set(labels.get("positive") or []), set(labels.get("negative") or [])
    both = pos & neg
    if both:
        sys.exit("ticket_triage: ids labelled both positive and negative: " + ", ".join(sorted(both)))
    truth = {i: True for i in pos}
    truth.update({i: False for i in neg})
    return truth


def evaluate(rows, truth):
    """Score labelled rows. Returns a dict; nothing here divides by zero.

    `rows` need `id` and `bucket`. Rows without a label are ignored; labelled ids with no row are
    listed as `missing`.
    """
    scored = [dict(r, label=truth[r["id"]]) for r in rows if r["id"] in truth]
    by_bucket = {b: {"positive": 0, "negative": 0} for b in BUCKETS}
    for r in scored:
        by_bucket[r["bucket"]]["positive" if r["label"] else "negative"] += 1
    pr_like = (PURE_PR, PROBABLY_PR)
    return {
        "n": len(scored),
        "positives": sum(r["label"] for r in scored),
        "negatives": sum(not r["label"] for r in scored),
        "by_bucket": by_bucket,
        "pure_pr": confusion((r["bucket"] == PURE_PR, r["label"]) for r in scored),
        "pure_or_probably_pr": confusion((r["bucket"] in pr_like, r["label"]) for r in scored),
        "misses": sorted("{}={}".format(r["id"], r["bucket"]) for r in scored if r["label"] and r["bucket"] not in pr_like),
        "false_alarms": sorted(r["id"] for r in scored if not r["label"] and r["bucket"] == PURE_PR),
        "missing": sorted(set(truth) - {r["id"] for r in rows}),
    }


def format_eval(ev):
    lines = ["labelled issues scored: {} ({} positive, {} negative)".format(ev["n"], ev["positives"], ev["negatives"]), "",
             "confusion by bucket (rows: bucket; columns: shipped a PR / closed without code)",
             "  {:18} {:>8} {:>8}".format("bucket", "positive", "negative")]
    for b in BUCKETS:
        c = ev["by_bucket"][b]
        lines.append("  {:18} {:>8} {:>8}".format(b, c["positive"], c["negative"]))
    for name, key in (("pure_pr", "pure_pr"), ("pure_pr or likely_pr_review", "pure_or_probably_pr")):
        m = ev[key]
        lines += ["", "called PR = " + name,
                  "  tp {tp}  fp {fp}  fn {fn}  tn {tn}".format(**m),
                  "  precision {}{}  recall {}{}  F1 {}  false-positive rate {}".format(
                      fmt_rate(m["precision"]), fmt_ci(m["precision_ci95"]), fmt_rate(m["recall"]),
                      fmt_ci(m["recall_ci95"]), fmt_rate(m["f1"]), fmt_rate(m["false_positive_rate"]))]
    lines += ["", "misses (shipped a PR, not called PR): " + (", ".join(ev["misses"]) or "none"),
              "false alarms (no PR, called pure_pr): " + (", ".join(ev["false_alarms"]) or "none")]
    if ev["missing"]:
        lines.append("labelled but not found: " + ", ".join(ev["missing"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------- triage cache


def triage_verdict(b, include_probably=True):
    if b == PURE_PR or (include_probably and b == PROBABLY_PR):
        return "CODE"
    if b in (NOT_PR, ALREADY_RESOLVED):
        return "NOT_CODE"
    return "AMBIGUOUS"


def emit_triage(results, path=os.path.join(".agent", "triage.json"), include_probably=True, now=None):
    """Merge buckets into the pipeline's triage cache. Rows already in the file are never overwritten.

    By default likely_pr_review becomes CODE alongside pure_pr. The run this tool was built for sent
    both buckets into the pipeline after a person reviewed the report; most shipped. Pass
    include_probably=False (--emit-exclude-likely) to make them AMBIGUOUS, which the pipeline skips.

    Schema: an object keyed by issue id, {"ENG-123": {"verdict", "reason", "priority", "by", "at"}}.
    An existing file in the older list form, [{"id", "priority", "verdict", "reason"}], is kept in
    that form and appended to.
    """
    try:
        triage = read_json(path)
    except (FileNotFoundError, ValueError):
        triage = {}
    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    as_list = isinstance(triage, list)
    existing = {row.get("id") for row in triage} if as_list else set(triage)
    added = 0
    for r in sorted(results, key=lambda r: r["id"]):
        if r["id"] in existing:
            continue
        path_answer = r["answers"]["path"]
        row = {
            "verdict": triage_verdict(r["bucket"], include_probably),
            "reason": "ticket-triage {}: path {} p(pr_only)={:.2f}".format(
                r["bucket"], path_answer["choice"], path_answer["probabilities"].get("pr_only", 0.0)),
            "priority": r.get("priority"),
        }
        if as_list:
            triage.append(dict(id=r["id"], **row))
        else:
            triage[r["id"]] = dict(row, by="ticket-triage", at=now)
        existing.add(r["id"])
        added += 1
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(triage, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return added


# --------------------------------------------------------------------------- config and CLI


def load_pipeline_config(path):
    try:
        return read_json(path)
    except (FileNotFoundError, ValueError):
        return {}


def load_repository(path):
    """repository.json: {"name", "contains": [...], "does_not_contain": [...], "deploy_includes": [...]}."""
    try:
        repo = read_json(path)
    except FileNotFoundError:
        sys.exit("ticket_triage: --repository {} not found (see examples/repository.example.json)".format(path))
    for key in REPOSITORY_KEYS:
        if key not in repo:
            sys.exit("ticket_triage: {} has no {!r}".format(path, key))
    out = {k: repo[k] for k in REPOSITORY_KEYS}
    out["deploy_includes"] = list(repo.get("deploy_includes") or [])
    return out


def require_env(*keys):
    for key in keys:
        if not os.environ.get(key):
            sys.exit("ticket_triage: {} is not set".format(key))


def parser(cfg):
    lin, gitcfg = cfg.get("linear") or {}, cfg.get("git") or {}
    ap = argparse.ArgumentParser(prog="python -m ticket_triage.triage",
                                 description="Sort tracker issues by whether a pull request alone can resolve them.")
    ap.add_argument("--repository", help="JSON describing what a PR here can and cannot change (required unless --rebucket)")
    ap.add_argument("--assignee", default=lin.get("user"), help="Linear user name to pull issues for (default: linear.user)")
    ap.add_argument("--states", default="Todo,Backlog", help="comma-separated workflow states (default: Todo,Backlog)")
    ap.add_argument("--ids", help="comma-separated issue ids to classify instead of an assignee query")
    ap.add_argument("--issue-key", default=linear.DEFAULT_ISSUE_KEY, help="regex an issue id must match (default: %(default)s)")
    ap.add_argument("--base", default=gitcfg.get("baseBranch") or "main",
                    help="base branch for code evidence; origin/<base> is used when it exists (default: git.baseBranch, else main)")
    ap.add_argument("--pathspec", action="append", default=[], help="limit the symbol search to these paths; repeatable")
    ap.add_argument("--pr-limit", type=int, default=1000, help="how many recent pull requests to index (default: 1000)")
    ap.add_argument("--artifact-pattern", action="append", default=[],
                    help="regex for build output names that are not source files; repeatable")
    ap.add_argument("--ignore-comment", action="append", default=list(lin.get("unparkIgnorePatterns") or []),
                    help="regex; comments matching it are dropped; repeatable")
    ap.add_argument("--cut-description", action="append", default=[],
                    help="regex; the description is cut at its first match; repeatable")
    ap.add_argument("--include-comment-authors", action="store_true", help="send comment authors' names (default: pseudonyms)")
    ap.add_argument("--include-labels", action="store_true", help="send the issue's label names (default: not sent; no question reads them)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="model id (default: %(default)s; pin a version once tuned)")
    ap.add_argument("--out", default=os.path.join(".agent", "ticket-triage"), help="output directory")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-evidence", action="store_true", help="omit code_evidence and its three questions (version 1 behaviour)")
    ap.add_argument("--rebucket", action="store_true", help="re-apply bucket() to stored answers without calling the model")
    ap.add_argument("--eval", metavar="LABELS", help="score against labels.json (with --rebucket: re-score the stored eval)")
    ap.add_argument("--emit-triage", nargs="?", const=os.path.join(".agent", "triage.json"), metavar="PATH",
                    help="merge verdicts into the pipeline's triage cache (default path: .agent/triage.json)")
    ap.add_argument("--emit-exclude-likely", action="store_true",
                    help="with --emit-triage, write likely_pr_review as AMBIGUOUS instead of CODE")
    ap.add_argument("--pipeline-config", default=os.path.join(".agent", "config.json"), help=argparse.SUPPRESS)
    return ap


def main(argv=None):
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--pipeline-config", default=os.path.join(".agent", "config.json"))
    known, _ = pre.parse_known_args(argv)
    args = parser(load_pipeline_config(known.pipeline_config)).parse_args(argv)

    with_evidence = not args.no_evidence
    tag = "v1" if args.no_evidence else "v2"
    results_path = os.path.join(args.out, "results.json")
    eval_path = os.path.join(args.out, "eval-{}.json".format(tag))
    failures = []

    if args.rebucket and args.eval:
        rows = rebucket(read_json(eval_path))
        write_json(eval_path, rows)
        print(format_eval(evaluate(rows, load_labels(args.eval))))
        return 0

    kw = {}
    if not args.rebucket:
        if not args.repository:
            sys.exit("ticket_triage: --repository is required (see examples/repository.example.json)")
        require_env("LINEAR_API_KEY", "TYPESAFE_API_KEY")
        kw = dict(
            backend=get_backend("jev", args.model),
            repo=load_repository(args.repository),
            policy=TextPolicy([re.compile(p, re.I) for p in args.ignore_comment],
                              [re.compile(p, re.I) for p in args.cut_description],
                              args.include_comment_authors),
            gatherer=CodeEvidence(args.base, args.pathspec, args.pr_limit, args.artifact_pattern) if with_evidence else None,
            with_evidence=with_evidence,
            include_labels=args.include_labels,
        )

    if args.eval:
        truth = load_labels(args.eval)
        issues = linear.fetch_by_ids(sorted(truth), args.issue_key)
        # Judged as of pickup: comments before Linear's startedAt only, issue-id links withheld.
        rows, failures = classify_all(issues, args.workers, as_of_pickup=True, **kw)
        write_json(eval_path, rows)
        print(format_eval(evaluate(rows, truth)))
        if failures:
            print("FAILED " + "; ".join(failures), file=sys.stderr)
        return 0

    if args.rebucket:
        results = rebucket(read_json(results_path))
    else:
        if args.ids:
            issues = linear.fetch_by_ids(args.ids.split(","), args.issue_key)
        else:
            if not args.assignee:
                sys.exit("ticket_triage: --assignee or --ids is required (no linear.user in the pipeline config)")
            issues = linear.fetch_issues(args.assignee, [s.strip() for s in args.states.split(",") if s.strip()])
        results, failures = classify_all(issues, args.workers, **kw)
        if failures:
            print("FAILED " + "; ".join(failures), file=sys.stderr)
    write_json(results_path, results)
    with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
        f.write(render_report(results, failures, args.model))
    tokens = sum((r.get("usage") or {}).get("input_tokens", 0) for r in results)
    print("WROTE {}/report.md and results.json: {} issues, {} input tokens".format(args.out, len(results), tokens))
    print(" ".join("{}={}".format(k, v) for k, v in bucket_counts(results).items()))
    if args.emit_triage:
        added = emit_triage(results, args.emit_triage, include_probably=not args.emit_exclude_likely)
        print("TRIAGE {}: {} row(s) added, existing rows kept".format(args.emit_triage, added))
    return 0


if __name__ == "__main__":
    sys.exit(main())
