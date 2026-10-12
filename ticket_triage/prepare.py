"""Clean ticket text before it is sent anywhere. Shared by triage and the labeler.

Ticket text goes from Linear to the model and to local output files; these tools never print it.
"""

DESC_LIMIT = 3500
COMMENT_LIMIT = 600
MAX_COMMENTS = 8


class TextPolicy(object):
    """What to strip and what to reveal.

    ignore_comments: compiled regexes; a comment matching any is dropped. Integrations that write
        under a person's account (a feedback tool syncing a status, say) add comments that carry no
        information about the work.
    cut_description: compiled regexes; the description is cut at the first match. Some intake tools
        append session metadata, console logs or replay links after the report itself.
    include_authors: send comment authors' real names. Off by default: authors become
        "person 1", "person 2", ... per issue, which keeps who-said-what without naming anyone.
    """

    def __init__(self, ignore_comments=(), cut_description=(), include_authors=False):
        self.ignore_comments = list(ignore_comments)
        self.cut_description = list(cut_description)
        self.include_authors = include_authors


def _clip(text, limit):
    return text[:limit] + (" [truncated]" if len(text) > limit else "")


def clean_description(text, policy):
    text = text or ""
    for pattern in policy.cut_description:
        m = pattern.search(text)
        if m:
            text = text[: m.start()]
    return _clip(text.strip(), DESC_LIMIT)


def visible_comments(issue, policy, before=None):
    """Comments in time order, without ignored ones; with `before`, only those created earlier."""
    nodes = ((issue.get("comments") or {}).get("nodes")) or []
    return [
        c for c in sorted(nodes, key=lambda c: c["createdAt"])
        if c.get("body") and not any(p.search(c["body"]) for p in policy.ignore_comments)
        and (before is None or c["createdAt"] < before)
    ]


def comment_list(comments, policy):
    """The last MAX_COMMENTS comments, clipped, with pseudonymous authors unless policy says otherwise.

    Returns (comments, earlier_comments_omitted).
    """
    truncated = len(comments) > MAX_COMMENTS
    names = {}
    out = []
    for c in comments[-MAX_COMMENTS:]:
        name = (c.get("user") or {}).get("name") or "unknown"
        if not policy.include_authors:
            name = names.setdefault(name, "person {}".format(len(names) + 1))
        out.append({"author": name, "text": _clip(c["body"], COMMENT_LIMIT)})
    return out, truncated


def issue_state(issue, comments, policy, include_labels=False):
    """The `issue` object the model reads. Labels are left out unless asked for: no question reads them."""
    clist, truncated = comment_list(comments, policy)
    out = {
        "title": issue["title"],
        "workflow_state": (issue.get("state") or {}).get("name"),
        "project": (issue.get("project") or {}).get("name"),
        "description": clean_description(issue.get("description"), policy),
        "comments": clist,
        "earlier_comments_omitted": truncated,
    }
    if include_labels:
        out["labels"] = [l["name"] for l in ((issue.get("labels") or {}).get("nodes") or [])]
    return out
