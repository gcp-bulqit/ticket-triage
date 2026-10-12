"""Code-side evidence, computed in code from the repository, never by the model.

For one issue:
  referenced_files    file paths the ticket text names, and whether each exists on the base branch
  referenced_symbols  backticked identifiers the ticket names, and in how many files they appear
  pull_requests       pull requests whose title or branch names the issue id (from `gh pr list`)
  merged_pr_count     how many of those were merged
  commits             base-branch commits whose message names the issue id

The model reads this as `code_evidence` beside the ticket. It exists because a model reading only
the ticket cannot see that a fix was merged under another ticket, or that the thing a ticket asks
for already exists.

Needs `git` in the target repository and an authenticated `gh` for pull requests (without `gh`,
the pull request list is simply empty).
"""

import json
import re
import subprocess
import sys
import threading

PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\."
    r"(?:tsx?|jsx?|mjs|cjs|py|rb|go|rs|java|kt|swift|cs|php|css|scss|html|vue|svelte|sql|md|json|ya?ml|toml))\b"
)
TICK_RE = re.compile(r"`([^`\n]{3,80})`")
SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{3,}$")
# Bundler output and stack-trace frames pasted into tickets are not source files. The defaults
# cover common JavaScript bundler output (framework build folders, chunks, node_modules) and very
# short minified names; add patterns for your stack with --artifact-pattern.
DEFAULT_ARTIFACT_PATTERNS = (
    r"(^|/)(__|\.next/|next/server|chunks/|node_modules/|dist/|build/)",
    r"\._\.",
    r"^[\w-]{1,3}\.\w+$",
)
MAX_REFS = 10


def refs_in(text, artifact_res=None):
    """Paths and code symbols the ticket text names, found by pattern, not by the model."""
    if artifact_res is None:
        artifact_res = [re.compile(p) for p in DEFAULT_ARTIFACT_PATTERNS]
    paths, symbols = [], []
    for m in PATH_RE.finditer(text or ""):
        p = m.group(1).strip("./")
        if p and p not in paths and not p.startswith("http") and not any(r.search(p) for r in artifact_res):
            paths.append(p)
    for m in TICK_RE.finditer(text or ""):
        tok = m.group(1).strip()
        if SYMBOL_RE.match(tok) and tok not in symbols:
            symbols.append(tok)
    return paths[:MAX_REFS], symbols[:MAX_REFS]


def names_issue(text, ident):
    """True when `text` names `ident` as a whole id: ENG-12 matches "ENG-12 fix", not "ENG-123"."""
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(ident) + r"(?![0-9])", text or "", re.I) is not None


class CodeEvidence(object):
    """Evidence gatherer bound to one repository checkout.

    base: branch name or ref. A bare name like `main` resolves to `origin/main` when that exists,
        else to the local branch.
    pathspec: limit the symbol search to these paths (empty: the whole repository).
    pr_limit: how many recent pull requests to index.
    artifact_patterns: extra regexes for names that are build output, not source.
    """

    def __init__(self, base="main", pathspec=(), pr_limit=1000, artifact_patterns=(), cwd=None):
        self.cwd = cwd
        self.base = self.resolve_base(base)
        self.pathspec = list(pathspec)
        self.pr_limit = pr_limit
        self.artifact_res = [re.compile(p) for p in list(DEFAULT_ARTIFACT_PATTERNS) + list(artifact_patterns)]
        self._lock = threading.Lock()
        self._files = None
        self._prs = None

    def git(self, *args):
        r = subprocess.run(["git"] + list(args), capture_output=True, text=True, cwd=self.cwd)
        return r.stdout if r.returncode == 0 else ""

    def resolve_base(self, base):
        for ref in ("origin/" + base, base) if "/" not in base else (base,):
            if subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                              capture_output=True, cwd=self.cwd).returncode == 0:
                return ref
        print("ticket_triage: base {!r} not found; file and commit evidence will be empty".format(base), file=sys.stderr)
        return base

    def tree_files(self):
        with self._lock:
            if self._files is None:
                self._files = self.git("ls-tree", "-r", "--name-only", self.base).splitlines()
            return self._files

    def pr_index(self):
        with self._lock:
            if self._prs is None:
                try:
                    r = subprocess.run(
                        ["gh", "pr", "list", "--state", "all", "--limit", str(self.pr_limit),
                         "--json", "number,title,state,headRefName,mergedAt"],
                        capture_output=True, text=True, cwd=self.cwd,
                    )
                    self._prs = json.loads(r.stdout) if r.returncode == 0 else []
                except (OSError, ValueError):
                    self._prs = []
            return self._prs

    def gather(self, issue, comments, use_issue_links=True):
        ident = issue["identifier"]
        text = " ".join([issue.get("title") or "", issue.get("description") or ""] + [c.get("body") or "" for c in comments])
        paths, symbols = refs_in(text, self.artifact_res)
        files = self.tree_files()
        ref_files = []
        for p in paths:
            hits = [f for f in files if f == p or f.endswith("/" + p)]
            ref_files.append({"name": p, "exists": bool(hits), "matches": hits[:3]})
        ref_syms = []
        for s in symbols:
            found = self.git("grep", "-l", "-w", "-I", s, self.base, "--", *self.pathspec).splitlines()
            ref_syms.append({"name": s, "found_in_files": len(found),
                             "examples": [f.split(":", 1)[-1] for f in found[:3]]})
        ev = {"referenced_files": ref_files, "referenced_symbols": ref_syms}
        if use_issue_links:
            prs = [
                {"number": p["number"], "title": p["title"], "state": p["state"], "merged": bool(p.get("mergedAt"))}
                for p in self.pr_index()
                if names_issue(p.get("title"), ident) or names_issue(p.get("headRefName"), ident)
            ]
            pattern = "(^|[^A-Za-z0-9])" + re.escape(ident) + "([^0-9]|$)"
            commits = self.git("log", self.base, "--oneline", "-i", "-E", "--grep=" + pattern, "-n", "5").splitlines()
            ev.update({"pull_requests": prs[:6], "merged_pr_count": sum(p["merged"] for p in prs), "commits": commits})
        return ev
