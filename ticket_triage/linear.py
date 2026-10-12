"""Read issues and their comments from Linear's GraphQL API.

Needs LINEAR_API_KEY (a personal API key; read scope is enough). Nothing here writes to Linear.
"""

import os
import re
import sys

from ._http import post_json

LINEAR_URL = "https://api.linear.app/graphql"

# Linear identifiers look like ENG-123: a team key, a dash, a number.
DEFAULT_ISSUE_KEY = r"[A-Z][A-Z0-9]*-\d+"

ISSUES_QUERY = """
query($f: IssueFilter, $after: String) {
  issues(filter: $f, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier title description priority updatedAt startedAt
      state { name } project { name } labels { nodes { name } }
      comments(first: 100) { nodes { createdAt body user { name } } }
    }
  }
}
"""


def query(q, variables, api_key=None):
    key = api_key or os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RuntimeError("LINEAR_API_KEY is not set")
    out = post_json(LINEAR_URL, {"query": q, "variables": variables}, {"Authorization": key})
    if out.get("errors"):
        # Error messages from GraphQL describe the query, not ticket content; keep only the messages.
        raise RuntimeError("Linear error: " + "; ".join(str(e.get("message")) for e in out["errors"]))
    return out["data"]


def _paged(f):
    issues, after = [], None
    while True:
        page = query(ISSUES_QUERY, {"f": f, "after": after})["issues"]
        issues += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return issues
        after = page["pageInfo"]["endCursor"]


def fetch_issues(assignee, states):
    """Issues assigned to the one Linear user with display name `assignee`, in workflow `states`."""
    users = query("query($n:String!){users(filter:{name:{eq:$n}}){nodes{id name}}}", {"n": assignee})["users"]["nodes"]
    if len(users) != 1:
        sys.exit("ticket_triage: expected one Linear user named {!r}, found {}".format(assignee, len(users)))
    return _paged({"assignee": {"id": {"eq": users[0]["id"]}}, "state": {"name": {"in": list(states)}}})


def parse_ids(ids, issue_key=DEFAULT_ISSUE_KEY):
    """Validate issue ids and group their numbers by team key: {"ENG": [1, 2], "OPS": [7]}."""
    pattern = re.compile(issue_key)
    teams = {}
    for i in ids:
        i = i.strip()
        if not i:
            continue
        if not pattern.fullmatch(i):
            sys.exit("ticket_triage: {!r} does not look like an issue id (pattern {})".format(i, issue_key))
        team, num = i.rsplit("-", 1)
        teams.setdefault(team, []).append(int(num))
    return teams


def fetch_by_ids(ids, issue_key=DEFAULT_ISSUE_KEY):
    """Issues by identifier, regardless of assignee or state. Ids may span several teams."""
    issues = []
    for team, nums in sorted(parse_ids(ids, issue_key).items()):
        issues += _paged({"number": {"in": nums}, "team": {"key": {"eq": team}}})
    return issues
