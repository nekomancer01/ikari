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
_QUOTE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)  # [ \t], not \s: \s ran across lines, quadratic on long posts
_QUOTE_NOTE = None  # quoted text is someone else's words
_CODE = re.compile(r"```.*?```", re.DOTALL)

# Obvious sarcasm: an explicit marker, an eye-roll emoji, or a stock positive frame
# ("gotta love...", "oh great") wrapped around something negative. Only used to flip
# text that would otherwise score positive; subtler sarcasm is left alone.
_SARCASM_MARKER = re.compile(
    r"(?<!\bthe )(?<!\ba )(?<!\byour )(?<!\bforgot )(?<!\bmissing )(?:^|(?<=\s))/s(?=\s|$|[.!)^*])"
    r"|#(?:sarcasm|sarcastic)\b|\bsaid no ?(?:one|body) ever\b",
    re.IGNORECASE,
)
_EYEROLL = re.compile("[\U0001F643\U0001F644]")  # upside-down face, rolling eyes
_SARCASM_FRAME = re.compile(
    r"\b(?:(?:i |i just |i really |just |don'?t you )?(?:love|adore) (?:it )?(?:when|how)"
    r"|(?:you )?(?:gotta|got to|have to|ya gotta) love"
    r"|oh,? (?:great|joy|goody|wonderful|fantastic|brilliant|wow)"
    r"|just what (?:i|we) (?:needed|wanted)|thanks (?:a lot|a bunch|for nothing)"
    r"|nothing (?:like|says)|yeah,? right|what could possibly go wrong|wow,? (?:thanks|just|what a)"
    r"|real(?:ly)? (?:smooth|classy|mature)|what a (?:surprise|shock)|colou?r me (?:shocked|surprised)"
    r"|(?:yay|hooray|woo+hoo+)\b)",
    re.IGNORECASE,
)
# Everyday mishaps VADER and the model read as neutral ("another update that breaks everything").
_COMPLAINT = re.compile(
    r"\b(?:break(?:s|ing)?|broke|broken|crash(?:es|ed|ing)?|bug(?:s|gy)?|glitch(?:es|y)?|lag(?:s|gy|ging)?"
    r"|freez(?:es|ing)|froze|stuck|delay(?:s|ed)?|cancel(?:l)?ed|outage|overtime|traffic)\b",
    re.IGNORECASE,
)


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


def _negative(cleaned: str) -> bool:
    if _COMPLAINT.search(cleaned):
        return True
    if _analyzer().polarity_scores(cleaned)["compound"] <= NEGATIVE_THRESHOLD:
        return True
    model = sentiment_model.load()
    if model is None:
        return False
    probs = model.probabilities(cleaned, vader_features(cleaned))
    return probs["negative"] > max(probs["positive"], 0.4)


def is_sarcastic(cleaned: str) -> bool:
    """True for obvious sarcasm in already-cleaned text (see the patterns above)."""
    if _SARCASM_MARKER.search(cleaned) or _EYEROLL.search(cleaned):
        return True
    if not _SARCASM_FRAME.search(cleaned):
        return False
    rest = _SARCASM_FRAME.sub(" ", cleaned).strip(" ,.!?")
    return bool(rest) and _negative(rest)


def score(text: str, scorer: str = "model") -> tuple[float, str]:
    """Return (score in [-1, 1], label). Falls back to VADER if the model isn't available.

    Text that reads positive but is obviously sarcastic is flipped to negative.
    """
    result = score_model(text) if scorer == "model" else None
    compound, label = result if result is not None else score_vader(text)
    if label == "positive" and is_sarcastic(clean(text)):
        return -compound, "negative"
    return compound, label
