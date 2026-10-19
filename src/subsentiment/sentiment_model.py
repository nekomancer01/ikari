"""Offline classifier: TF-IDF word/bigram features plus VADER scores, linear on top.

The weights ship as ``data/sentiment_model.json.gz`` (built by ``tools/train_model.py``)
and are applied in pure Python, so scikit-learn is only needed for training.
"""
from __future__ import annotations

import gzip
import json
import math
import re
from functools import lru_cache
from importlib import resources

_TOKEN = re.compile(r"(?u)\b\w\w+\b")
MODEL_FILE = "sentiment_model.json.gz"


def tokens(text: str) -> list[str]:
    words = _TOKEN.findall(text.lower())
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


class Model:
    def __init__(self, data: dict):
        self.classes: list[str] = data["classes"]
        self.vocab: dict[str, list] = data["vocab"]  # term -> [idf, w_class0, w_class1, ...]
        self.vader_w: list[list[float]] = data["vader_weights"]  # per class, one weight per vader feature
        self.bias: list[float] = data["bias"]

    def probabilities(self, cleaned: str, vader: list[float]) -> dict[str, float]:
        counts: dict[str, int] = {}
        for t in tokens(cleaned):
            if t in self.vocab:
                counts[t] = counts.get(t, 0) + 1
        weights = {t: (1 + math.log(c)) * self.vocab[t][0] for t, c in counts.items()}
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        logits = []
        for k in range(len(self.classes)):
            z = self.bias[k] + sum(w * f for w, f in zip(self.vader_w[k], vader))
            z += sum(w / norm * self.vocab[t][1 + k] for t, w in weights.items())
            logits.append(z)
        m = max(logits)
        exps = [math.exp(z - m) for z in logits]
        s = sum(exps)
        return {c: e / s for c, e in zip(self.classes, exps)}


@lru_cache(maxsize=1)
def load() -> Model | None:
    """The bundled model, or None if it isn't packaged."""
    try:
        with resources.files("subsentiment.data").joinpath(MODEL_FILE).open("rb") as f:
            return Model(json.loads(gzip.decompress(f.read())))
    except (FileNotFoundError, ModuleNotFoundError):
        return None
