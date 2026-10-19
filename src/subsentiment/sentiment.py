"""Sentiment scoring with VADER, which is tuned for short social-media text."""
from __future__ import annotations

import re
from functools import lru_cache

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

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


def score(text: str) -> tuple[float, str]:
    """Return (compound score in [-1, 1], label)."""
    cleaned = clean(text)
    if not cleaned:
        return 0.0, "neutral"
    compound = _analyzer().polarity_scores(cleaned)["compound"]
    return compound, label_for(compound)
