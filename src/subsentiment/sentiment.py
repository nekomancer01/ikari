"""Sentiment scoring with VADER, which is tuned for short social-media text."""
from __future__ import annotations

import copy
import re
from functools import lru_cache

from vaderSentiment import vaderSentiment as vader_lib
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from subsentiment import sentiment_model

# Standard VADER cut-offs on the compound score.
POSITIVE_THRESHOLD = 0.05
NEGATIVE_THRESHOLD = -0.05

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


@lru_cache(maxsize=1)
def _known_words() -> frozenset[str]:
    """Every word VADER or the classifier (alone or in a pair) reads anything into."""
    words = set(_analyzer().lexicon) | set(vader_lib.BOOSTER_DICT) | set(vader_lib.NEGATE)
    model = sentiment_model.load()
    if model is not None:
        words.update(w for term in model.vocab for w in term.split())
    return frozenset(words)


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


def vader_features(cleaned: str, p: dict | None = None) -> list[float]:
    """What the classifier takes from VADER's scores of cleaned text (or ``p``, ones worked out already)."""
    if p is None:
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


def _negative(cleaned: str, lexicon: Lexicon | None = None) -> bool:
    """Whether cleaned text reads negative, for the sarcasm check; with ``lexicon``, its set words as set there."""
    if lexicon is None or not lexicon.uses(cleaned):
        if _COMPLAINT.search(cleaned):
            return True
        if _analyzer().polarity_scores(cleaned)["compound"] <= NEGATIVE_THRESHOLD:
            return True
        model = sentiment_model.load()
        if model is None:
            return False
        probs = model.probabilities(cleaned, vader_features(cleaned))
    else:
        if _COMPLAINT.search(lexicon.blanked(cleaned)):  # (a complaint word set to mean something else isn't one)
            return True
        p, probs = lexicon.read(cleaned)
        if p["compound"] <= NEGATIVE_THRESHOLD:
            return True
        if probs is None:
            return False
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
    BLANK = "zqikari"  # stands for a set word with no meaning of its own (stand-ins add a letter or more)

    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = {self.key(w): max(-1.0, min(1.0, float(v))) for w, v in (weights or {}).items() if self.key(w)}
        self._find = None
        if self.weights:  # longest first, so a phrase wins over a word inside it
            words = sorted(self.weights, key=len, reverse=True)
            self._find = re.compile(r"(?<![\w'])(?:" + "|".join(
                r"\s+".join(map(re.escape, w.split())) for w in words) + r")(?![\w'])", re.IGNORECASE)
            # Letters only, so VADER keeps them whole, and nothing anyone writes. By casefold, as the search
            # ignores case the same way ("ſtuff" is found for "stuff"). A word set to neutral is BLANK, which VADER
            # doesn't know: a known word, even of valence 0, takes a lift from a "very" or "completely" before it.
            self._stand_in = {w.casefold(): self.BLANK + "".join(chr(97 + int(d)) for d in str(i))
                              if self.weights[w] else self.BLANK for i, w in enumerate(words)}
            self._spaced = {w.casefold(): bool(self.weights[w]) for w in words}
            self._analyzer = copy.copy(_analyzer())
            self._analyzer.lexicon = {**_analyzer().lexicon,
                                      **{self._stand_in[w.casefold()]: v * self.VALENCE for w, v in self.weights.items() if v}}
        else:
            self._analyzer = _analyzer()

    @staticmethod
    def key(word: str) -> str:
        """How a word or phrase is kept: lowercased, with single spaces."""
        return " ".join(word.lower().split())

    @staticmethod
    def known(word: str) -> bool:
        """Whether either scorer reads anything into ``word`` (or a word of a phrase). One neither knows already
        reads as neutral, so setting it to neutral changes no score."""
        key = Lexicon.key(word)
        return any(w in _known_words() for w in (key, *key.split(), *sentiment_model.tokens(key)))

    def uses(self, text: str) -> bool:
        """Whether ``text`` may use any of the set words: it's searched as written, so one only in a quote or a
        link (which scoring leaves out) counts too."""
        return bool(self._find and self._find.search(text))

    def score(self, text: str, scorer: str = "model") -> tuple[float, str]:
        """Like ``score``, reading the set words as set here."""
        cleaned = clean(text)
        if not self.uses(cleaned):  # no set word in it
            return score(text, scorer)
        p, probs = self.read(cleaned, scorer)
        if probs is None:
            compound, label = p["compound"], label_for(p["compound"])
        else:
            compound, label = probs["positive"] - probs["negative"], max(probs, key=probs.get)
        if label == "positive" and is_sarcastic(cleaned, lambda rest: _negative(rest, self)):
            return -compound, "negative"
        return compound, label

    def read(self, cleaned: str, scorer: str = "model") -> tuple[dict, dict | None]:
        """VADER's scores of cleaned text using a set word, and the classifier's odds (None without it)."""
        p = self._analyzer.polarity_scores(self._find.sub(self._swap, cleaned))
        model = sentiment_model.load() if scorer == "model" else None
        if model is None:
            return p, None
        # The set words as no word at all: one neither scorer knows, so the classifier doesn't pair the
        # words either side of one ("running dungeon content" isn't "running content") and VADER's
        # "not" and "very" stay as far from what they reach.
        plain = self.blanked(cleaned)
        without = self._analyzer.polarity_scores(plain)["compound"]
        shift = max(-self.MOST, min(self.MOST, self.NUDGE * (p["compound"] - without)))
        return p, model.probabilities(plain, vader_features(cleaned, p), shift)

    _made: dict = {}  # the last lexicons ``compare`` made, by their words, so each process makes them once

    @classmethod
    def compare(cls, weights: dict[str, float], words: frozenset[str], texts: list[str]) -> list[tuple[float, str, str]]:
        """Each of ``texts`` as (score, label) by ``weights``, and the label it gets with ``words`` meaning what
        they usually do instead, to tell what setting them changed. Rescoring runs it in other processes."""
        key = (tuple(sorted(weights.items())), words)
        made = cls._made.get(key)
        if made is None:
            cls._made.clear()
            made = cls._made[key] = (cls(weights), cls({w: v for w, v in weights.items() if w not in words}))
        lexicon, without = made
        return [(*(got := lexicon.score(t)), without.score(t)[1] if words else got[1]) for t in texts]

    def blanked(self, cleaned: str) -> str:
        """Cleaned text with each set word as BLANK: as if it were a word neither scorer knows."""
        return self._find.sub(lambda m: self._swap(m, blank=True), cleaned)

    def _swap(self, m: re.Match, blank: bool = False) -> str:
        """A set word's stand-in (or BLANK), in capitals where it was written in them, as VADER weighs those. One
        that means something is spaced off, so VADER reads it apart from what's joined on ("grind-heavy"); a
        neutral one isn't, so it reads just as the word did."""
        word = self.key(m.group()).casefold()
        stand_in = self.BLANK if blank else self._stand_in.get(word)
        if stand_in is None:
            return m.group()
        stand_in = stand_in.upper() if m.group().isupper() else stand_in
        return f" {stand_in} " if self._spaced.get(word) else stand_in




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
