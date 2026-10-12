"""A user-supplied label taxonomy, written so a model can classify against it.

The taxonomy is data, not code: a JSON file you own (see examples/taxonomy.example.json).

  groups       {group: {"question": str, "labels": {label: description}}}
               One typed choice question per group. Write every description in one shape: what it
               is, what it excludes, and its nearest neighbour. The neighbour clause is the
               load-bearing part: a classifier picking between two close labels needs to know where
               the line falls, not what each one means in isolation. Give a group a "none" label
               when nothing may fit.
  dimensions   {name: {"type": "noul"|"score", "instructions", "criteria", "truth": outcome|null}}
               Scoring dimensions, asked separately and kept separately so a routing rule can change
               without re-running anything. `truth` names the recorded outcome that scores it
               (pr, decision, human_answer, cycles); a dimension with no truth is reported but never
               claimed to be validated.
  historic     {tracker label: [group, label] | null}   optional
               Folds the labels your tracker uses today onto this shape, so a backtest can score
               against tickets labelled before the taxonomy existed. null means the old label
               carries no information (a dumping ground; scoring against it would measure the
               dumping, not the label). Tracker labels whose name is already a label in exactly one
               group map to it without an entry.
  effort_bands [[upper edge, band name], ...]           optional
               Bins the `effort` dimension's score into your team's effort bands, in code. The
               shipped edges are uncalibrated; fit yours with `label --calibrate-effort`.
"""

import json

DIMENSION_TYPES = ("noul", "score")


class TaxonomyError(ValueError):
    pass


class Taxonomy(object):
    def __init__(self, data):
        self.groups = data.get("groups") or {}
        self.dimensions = data.get("dimensions") or {}
        self.historic = data.get("historic") or {}
        self.effort_bands = [(float(e), str(n)) for e, n in (data.get("effort_bands") or [])]
        self.check()

    # ----------------------------------------------------------------- validation

    def check(self):
        if not isinstance(self.groups, dict) or not isinstance(self.dimensions, dict):
            raise TaxonomyError("`groups` and `dimensions` must be objects")
        if not self.groups and not self.dimensions:
            raise TaxonomyError("a taxonomy needs at least one group or dimension")
        for name, g in self.groups.items():
            if not isinstance(g, dict) or not str(g.get("question") or "").strip():
                raise TaxonomyError("group {!r} has no question".format(name))
            labels = g.get("labels")
            if not isinstance(labels, dict) or len(labels) < 2:
                raise TaxonomyError("group {!r} needs at least two labels".format(name))
            for label, desc in labels.items():
                if not isinstance(desc, str) or not desc.strip():
                    raise TaxonomyError("label {!r} in group {!r} has no description".format(label, name))
        for name, d in self.dimensions.items():
            if d.get("type") not in DIMENSION_TYPES:
                raise TaxonomyError("dimension {!r}: type must be one of {}".format(name, DIMENSION_TYPES))
            if not d.get("instructions"):
                raise TaxonomyError("dimension {!r} has no instructions".format(name))
            c = d.get("criteria")
            if d["type"] == "noul" and not (isinstance(c, dict) and c.get("true") and c.get("false")):
                raise TaxonomyError("noul dimension {!r} needs criteria with 'true' and 'false'".format(name))
            if d["type"] == "score" and not (isinstance(c, list) and len(c) >= 2):
                raise TaxonomyError("score dimension {!r} needs a list of at least two levels".format(name))
        band_names = [n for _, n in self.effort_bands]
        edges = [e for e, _ in self.effort_bands]
        if edges != sorted(edges) or len(set(edges)) != len(edges) or len(set(band_names)) != len(band_names):
            raise TaxonomyError("effort_bands must have increasing edges and unique names")
        for old, target in self.historic.items():
            if target is None:
                continue
            if not (isinstance(target, list) and len(target) == 2):
                raise TaxonomyError("historic {!r} must map to [group, label] or null".format(old))
            group, label = target
            if group == "effort":
                if label not in band_names:
                    raise TaxonomyError("historic {!r} maps to unknown effort band {!r}".format(old, label))
            elif group not in self.groups or label not in self.groups[group]["labels"]:
                raise TaxonomyError("historic {!r} maps to unknown {}/{}".format(old, group, label))

    # ----------------------------------------------------------------- questions

    def label_questions(self, groups=None):
        """One typed choice question per label group, criteria taken from the descriptions."""
        return {
            "label_" + name: {"type": "choice", "instructions": g["question"], "criteria": dict(g["labels"])}
            for name, g in self.groups.items() if not groups or name in groups
        }

    def dimension_questions(self, dims=None):
        return {
            name: {k: v for k, v in d.items() if k in ("type", "instructions", "criteria")}
            for name, d in self.dimensions.items() if not dims or name in dims
        }

    # ----------------------------------------------------------------- mapping

    def historic_expected(self, label_names):
        """Fold a ticket's tracker labels into this shape: {group: label}."""
        exp = {}
        band_names = {n for _, n in self.effort_bands}
        for n in label_names:
            if n in self.historic:
                m = self.historic[n]
                if m:
                    exp[m[0]] = m[1]
                continue
            owners = [g for g, spec in self.groups.items() if n in spec["labels"]]
            if len(owners) == 1:
                exp[owners[0]] = n
            elif n in band_names:
                exp["effort"] = n
        return exp

    def effort_band(self, score):
        """Bin an effort magnitude into a band. None when the taxonomy has no bands."""
        if not self.effort_bands:
            return None
        for edge, name in self.effort_bands:
            if score < edge:
                return name
        return self.effort_bands[-1][1]


def load_taxonomy(path):
    with open(path, encoding="utf-8") as f:
        return Taxonomy(json.load(f))
