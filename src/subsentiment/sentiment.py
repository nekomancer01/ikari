"""Sentiment scoring with VADER, which is tuned for short social-media text."""
from __future__ import annotations

import re
from functools import lru_cache

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from subsentiment import sentiment_model

# Standard VADER cut-offs on the compound score.
POSITIVE_THRESHOLD = 0.05
NEGATIVE_THRESHOLD = -0.05

LABELS = ("negative", "neutral", "positive")

_URL = re.compile(r"https?://\S+")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_QUOTE = re.compile(r"^\s*>.*$", re.MULTILINE)  # quoted text is someone else's words
_CODE = re.compile(r"```.*?```", re.DOTALL)


@lru_cache(maxsize=1)
def _analyzer() -> SentimentIntensityAnalyzer:
    return SentimentIntensityAnalyzer()


def clean(text: str) -> str:
    text = _CODE.sub(" ", text)
    text = _QUOTE.sub(" ", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    return " ".join(text.split())


def label_for(compound: float) -> str:
    if compound >= POSITIVE_THRESHOLD:
        return "positive"
    if compound <= NEGATIVE_THRESHOLD:
        return "negative"
    return "neutral"


def vader_features(cleaned: str) -> list[float]:
    p = _analyzer().polarity_scores(cleaned) if cleaned else {"neg": 0, "neu": 1, "pos": 0, "compound": 0}
    return [p["neg"], p["neu"], p["pos"], p["compound"], abs(p["compound"])]


def score_vader(text: str) -> tuple[float, str]:
    """Plain VADER: (compound score in [-1, 1], label from the +/-0.05 cut-offs)."""
    cleaned = clean(text)
    if not cleaned:
        return 0.0, "neutral"
    compound = _analyzer().polarity_scores(cleaned)["compound"]
    return compound, label_for(compound)


def score_model(text: str) -> tuple[float, str] | None:
    """Bundled classifier: (P(positive) - P(negative), most likely label), or None if not packaged."""
    model = sentiment_model.load()
    if model is None:
        return None
    cleaned = clean(text)
    if not cleaned:
        return 0.0, "neutral"
    probs = model.probabilities(cleaned, vader_features(cleaned))
    return probs["positive"] - probs["negative"], max(probs, key=probs.get)


def score(text: str, scorer: str = "model") -> tuple[float, str]:
    """Return (score in [-1, 1], label). Falls back to VADER if the model isn't available."""
    if scorer == "model":
        result = score_model(text)
        if result is not None:
            return result
    return score_vader(text)
