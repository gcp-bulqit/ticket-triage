"""The only module that calls a model. Everything that leaves for TypeSafe leaves through `JevBackend.ask`.

A backend takes a `state` (any JSON value: what the model reads) and a map of typed questions, and
returns typed answers. The questions use TypeSafe's System One primitives:

  choice  {"type": "choice", "instructions": ..., "criteria": {option: description, ...}}
          -> {"choice": option, "probabilities": {option: p, ...}, "confidence": c}
  noul    {"type": "noul", "instructions": ..., "criteria": {"true": ..., "false": ...}}
          -> {"noul": p}                      (probability of yes; there is no separate confidence)
  score   {"type": "score", "instructions": ..., "criteria": [level 0, level 1, ...]}
          -> {"score": s, "probabilities": [...], "confidence": c}
             (s is probability-weighted and can fall between levels; threshold it, do not
              interpolate exact magnitudes from it)

Question keys are for code and are not sent to the model, so each question carries its full
meaning in its instructions. All questions in one call are answered independently of each other.

To use another model, write a class with the same `ask` signature that returns the same answer
shapes, and pass it wherever a backend is accepted. Only Jev is implemented here.

Needs TYPESAFE_API_KEY. TypeSafe is a commercial, paid, third-party service; see
THIRD_PARTY_NOTICES.md for what is sent.
"""

import os

from ._http import post_json

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
# An alias that tracks the current Jev release. Once thresholds are tuned against a version, pin
# that versioned id with --model (the id each response reports is stored in every result row).
DEFAULT_MODEL = "jev-latest"


class Backend(object):
    """Interface: answer typed questions about a state."""

    model = None

    def ask(self, state, questions):
        """Return {"model": str, "answers": {key: answer}, "usage": {...} or None}."""
        raise NotImplementedError


class JevBackend(Backend):
    def __init__(self, model=DEFAULT_MODEL, api_key=None, url=TYPESAFE_URL):
        self.model = model
        self.url = url
        self._api_key = api_key

    def ask(self, state, questions):
        key = self._api_key or os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise RuntimeError("TYPESAFE_API_KEY is not set")
        res = post_json(
            self.url,
            {"state": state, "model": self.model, "questions": questions},
            {"Authorization": "Bearer " + key},
        )
        return {"model": res.get("model", self.model), "answers": res["answers"], "usage": res.get("usage")}


def get_backend(name="jev", model=DEFAULT_MODEL):
    if name == "jev":
        return JevBackend(model=model)
    raise ValueError("unknown backend {!r}; only 'jev' is implemented".format(name))
