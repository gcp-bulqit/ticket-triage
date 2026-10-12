"""Triage and label tracker issues for an agent pipeline, with typed model judgments and policy in code.

Modules:
  linear     fetch issues and comments from Linear's GraphQL API (LINEAR_API_KEY)
  evidence   code-side evidence from git and gh: PRs and commits naming an issue, and whether the
             files and symbols a ticket mentions exist on the base branch
  jev        the only module that calls a model (TypeSafe's Jev, TYPESAFE_API_KEY)
  prepare    shared cleaning of ticket text before it leaves your machine
  triage     can a pull request alone resolve this issue? (python -m ticket_triage.triage)
  label      infer labels and scoring dimensions from a user-supplied taxonomy
             (python -m ticket_triage.label)
  taxonomy   load and check a taxonomy file
  metrics    confusion counts, precision, recall, F1 and Wilson intervals

Standard library only; Python 3.8 or later.
"""

__version__ = "0.1.0"
