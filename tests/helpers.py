"""Shared fixtures for the offline tests. Nothing here touches the network."""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EXAMPLES = os.path.join(ROOT, "examples")


def example(name):
    with open(os.path.join(EXAMPLES, name), encoding="utf-8") as f:
        return json.load(f)


def example_path(name):
    return os.path.join(EXAMPLES, name)


class FakeBackend(object):
    """Stands in for the model. Records every state and question set it is asked."""

    model = "fake-1"

    def __init__(self, answer_fn):
        self.answer_fn = answer_fn
        self.calls = []

    def ask(self, state, questions):
        self.calls.append((state, questions))
        return {"model": self.model, "answers": self.answer_fn(state, questions), "usage": {"input_tokens": 10}}
