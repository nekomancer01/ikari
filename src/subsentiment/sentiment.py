"""Sentiment scoring with VADER, which is tuned for short social-media text."""
from __future__ import annotations

import copy
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
_QUOTE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)  # someone else's words ([ \t]: \s ran across lines, slow)
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


def is_sarcastic(cleaned: str, negative=_negative) -> bool:
    """True for obvious sarcasm in already-cleaned text (see the patterns above). ``negative`` says whether
    what a positive frame is wrapped around reads negative."""
    if _SARCASM_MARKER.search(cleaned) or _EYEROLL.search(cleaned):
        return True
    if not _SARCASM_FRAME.search(cleaned):
        return False
    rest = _SARCASM_FRAME.sub(" ", cleaned).strip(" ,.!?")
    return bool(rest) and negative(rest)


class Lexicon:
    """A subreddit's own meanings for words and phrases, each set by the user from -1 (reads negative there)
    through 0 (carries no sentiment there) to +1 (reads positive): "kill" in r/linux, a game's slang in its
    subreddit. Scoring takes each set word's usual meaning out and puts the user's in: VADER reads a stand-in
    word with that valence where it was (so "not" and "very" still apply to it), and the classifier, which
    doesn't see the word itself any more, has its positive and negative odds moved by as much as the stand-ins
    moved VADER's score. Text using none of them scores exactly as ``score`` has it."""

    VALENCE = 3.5  # VADER's valence of a word set to +1 (its lexicon runs about -4 to +4)
    NUDGE = 3.0  # how far the classifier's positive and negative logits move per unit the stand-ins moved VADER's
    MOST = 4.0  # the furthest they move

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = {self.key(w): max(-1.0, min(1.0, float(v))) for w, v in (weights or {}).items() if self.key(w)}
        self._find = None
        if self.weights:  # longest first, so a phrase wins over a word inside it
            words = sorted(self.weights, key=len, reverse=True)
            self._find = re.compile(r"(?<![\w'])(?:" + "|".join(
                r"\s+".join(map(re.escape, w.split())) for w in words) + r")(?![\w'])", re.IGNORECASE)
            # Letters only, so VADER keeps them whole, and nothing anyone writes. By casefold, as the search
            # ignores case the same way ("ſtuff" is found for "stuff").
            self._stand_in = {w.casefold(): "zqikari" + "".join(chr(97 + int(d)) for d in str(i))
                              for i, w in enumerate(words)}
            self._analyzer = copy.copy(_analyzer())
            self._analyzer.lexicon = {**_analyzer().lexicon,
                                      **{self._stand_in[w.casefold()]: v * self.VALENCE for w, v in self.weights.items()}}
        else:
            self._analyzer = _analyzer()

    @staticmethod
    def key(word: str) -> str:
        """How a word or phrase is kept: lowercased, with single spaces."""
        return " ".join(word.lower().split())

    def uses(self, text: str) -> bool:
        """Whether ``text`` may use any of the set words: it's searched as written, so one only in a quote or a
        link (which scoring leaves out) counts too."""
        return bool(self._find and self._find.search(text))

    def score(self, text: str, scorer: str = "model") -> tuple[float, str]:
        """Like ``score``, reading the set words as set here."""
        cleaned = clean(text)
        marked = self._find.sub(self._swap, cleaned) if self._find else cleaned
        if marked == cleaned:  # no set word in it
            return score(text, scorer)
        p = self._analyzer.polarity_scores(marked)
        model = sentiment_model.load() if scorer == "model" else None
        if model is None:
            compound, label = p["compound"], label_for(p["compound"])
        else:
            plain = " ".join(self._find.sub(" ", cleaned).split())
            without = self._analyzer.polarity_scores(plain)["compound"] if plain else 0.0
            shift = max(-self.MOST, min(self.MOST, self.NUDGE * (p["compound"] - without)))
            probs = model.probabilities(plain, [p["neg"], p["neu"], p["pos"], p["compound"], abs(p["compound"])], shift)
            compound, label = probs["positive"] - probs["negative"], max(probs, key=probs.get)
        if label == "positive" and is_sarcastic(cleaned, self._negative):
            return -compound, "negative"
        return compound, label

    def _swap(self, m: re.Match) -> str:
        stand_in = self._stand_in.get(self.key(m.group()).casefold())
        return m.group() if stand_in is None else f" {stand_in} "

    def _negative(self, rest: str) -> bool:
        """Whether what a sarcastic frame is wrapped around reads negative, with the set words as set here."""
        return self.score(rest)[1] == "negative" if self.uses(rest) else _negative(rest)


def score(text: str, scorer: str = "model") -> tuple[float, str]:
    """Return (score in [-1, 1], label). Falls back to VADER if the model isn't available.

    Text that reads positive but is obviously sarcastic is flipped to negative. A subreddit's own word
    meanings are read by Lexicon.score.
    """
    result = score_model(text) if scorer == "model" else None
    compound, label = result if result is not None else score_vader(text)
    if label == "positive" and is_sarcastic(clean(text)):
        return -compound, "negative"
    return compound, label
