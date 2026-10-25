"""Property tests (Hypothesis): thousands of generated inputs, odd ones included, for the code that takes what the
archive sends, cuts time ranges and scores text. A failure prints the smallest input that breaks the rule."""
import math

from hypothesis import example, given, settings, strategies as st

from subsentiment import arctic_shift as a
from subsentiment.reddit import Comment, normalize_subreddit
from subsentiment.sentiment import Lexicon, score

DAY = 86400
# What json.loads can hand back: any mix of these, nested.
JSON = st.recursive(st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
                    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
                    max_leaves=20)
# Records shaped like the archive's, with any of their fields missing or holding the wrong kind of value.
FIELDS = ["kind", "data", "replies", "children", "count", "depth", "id", "body", "score", "created_utc", "key",
          "date", "url", "selftext", "post_hint", "is_gallery", "title", "author", "permalink", "subreddit",
          "retrieved_on", "_meta", "num_comments", "upvote_ratio", "removed_by_category"]
RECORD = st.recursive(st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=12)
                      | st.sampled_from(["t1", "more", "image", "true", "2024-01-02", "1704153600000"]),
                      lambda inner: st.lists(inner, max_size=3)
                      | st.dictionaries(st.sampled_from(FIELDS), inner, max_size=6),
                      max_leaves=25)
WHEN = st.integers(0, 2_000_000_000)


# A comment tree as /api/comments/tree sends it (get() has already dropped records that aren't dicts): items
# {"kind": "t1", "data": {..., "replies": Listing}} and "more" stubs, any part of it missing or the wrong shape.
JUNK = st.none() | st.booleans() | st.integers() | st.text(max_size=4) | st.lists(st.integers(), max_size=2)
TREE = st.lists(st.recursive(
    st.fixed_dictionaries({"kind": st.sampled_from(["t1", "more"]) | JUNK}, optional={"data": JUNK}),
    lambda item: st.fixed_dictionaries({"kind": st.sampled_from(["t1", "more"]) | JUNK}, optional={
        "data": JUNK | st.fixed_dictionaries({}, optional={
            "id": JUNK, "depth": JUNK, "count": JUNK, "children": st.lists(st.text(max_size=3), max_size=2) | JUNK,
            "replies": JUNK | st.fixed_dictionaries({}, optional={
                "data": JUNK | st.fixed_dictionaries({}, optional={"children": st.lists(item, max_size=2) | JUNK})})})}),
    max_leaves=6), max_size=3)


@given(TREE)
@example([{"kind": "more", "data": True}])
@example([{"kind": "t1", "data": {"replies": {"data": [1]}}}])
def test_reading_a_comment_tree_never_fails(tree):
    for d in a._walk(tree):
        assert isinstance(d, dict) and isinstance(d["depth"], int)
    assert a._collapsed(tree) in (True, False)


@given(st.dictionaries(st.sampled_from(FIELDS), RECORD, max_size=12), st.text(min_size=1, max_size=10))
@example({"created_utc": float("inf"), "url": 7}, "p")  # json.loads reads "Infinity"
def test_reading_a_post_never_fails(d, pid):
    post = a._post({**d, "id": pid})
    assert post.id == pid and isinstance(post.title, str) and isinstance(post.image, bool)
    assert post.score is None or isinstance(post.score, int)
    assert math.isfinite(post.created_utc)


@given(RECORD)
@example({"key": float("nan")})
@example({"created_utc": "1" * 400})
def test_a_date_bucket_is_a_time_or_none(b):
    t = a._bucket_time(b)
    assert t is None or math.isfinite(t)


@given(WHEN, st.integers(1, 400 * DAY), st.integers(1, 64))
def test_slices_cover_the_range_back_to_back(after, length, n):
    got = a.slices(after, after + length, n)
    assert got[0][0] == after and got[-1][1] == after + length
    assert all(lo < hi for lo, hi in got) and all(x[1] == y[0] for x, y in zip(got, got[1:]))
    assert len(got) <= n


@given(st.integers(0, 10**6), st.integers(1, 200))
def test_quotas_share_the_total_evenly(total, n):
    got = a.quotas(total, n)
    assert len(got) == n and sum(got) == total and max(got) - min(got) <= 1


COMMENTS = st.lists(st.builds(lambda i, s: Comment(f"c{i}", "p", "sub", "t3_p", "", "", s, 0.0, 0, ""),
                              st.integers(0, 10**6), st.none() | st.integers(-100, 100)),
                    max_size=40, unique_by=lambda c: c.id)


@given(COMMENTS, st.integers(1, 50), st.integers(0, 20))
def test_a_random_sample_keeps_what_it_picked_when_asked_for_more(comments, n, more):
    picked = a.keep(comments, n, "random", "p")
    assert len(picked) == min(n, len(comments))
    assert a.keep(comments, n + more, "random", "p")[:len(picked)] == picked
    assert a.keep(list(reversed(comments)), n, "random", "p") == picked  # whatever order they came in


@given(COMMENTS, st.integers(1, 50))
def test_top_comments_come_best_first_with_unscored_last(comments, n):
    picked = a.keep(comments, n, "top", "p")
    scores = [c.score for c in picked]
    known = [s for s in scores if s is not None]
    assert known == sorted(known, reverse=True) and scores[:len(known)] == known
    if len(picked) < len(comments):  # nothing left out beats what was kept
        floor = min(known) if len(known) == len(scores) else None
        assert all(c.score is None or floor is None or c.score <= floor for c in comments if c not in picked)


@given(st.text(max_size=40))
def test_a_subreddit_name_is_normalised_once_and_for_all(name):
    once = normalize_subreddit(name)
    assert normalize_subreddit(once) == once


TEXT = st.text(st.characters(codec="utf-8"), max_size=200) | st.lists(
    st.sampled_from(["good", "bad", "not", "great", "terrible", "love", "hate", "lol", "!!", "> quoted", "`code`",
                     "[link](http://x.y)", "http://a.b/c", "yeah right", "😂", "very", "no", "GREAT", "\n"]),
    max_size=30).map(" ".join)


def agrees(compound: float, label: str) -> bool:
    return math.isfinite(compound) and -1 <= compound <= 1 and label in ("positive", "neutral", "negative") and (
        label != "positive" or compound > 0) and (label != "negative" or compound < 0)


@settings(deadline=None)
@given(TEXT)
def test_any_text_scores_in_range_with_a_label_that_agrees(text):
    for scorer in ("model", "vader"):
        assert agrees(*score(text, scorer))


@settings(deadline=None, max_examples=50)
@given(st.dictionaries(st.sampled_from(["grind", "nerf", "buff", "dead", "mid", "goat", "ratio", "cope"]),
                       st.floats(-1, 1), min_size=1, max_size=4), TEXT)
def test_a_subreddits_own_words_score_in_range_too(weights, text):
    lexicon = Lexicon(weights)
    assert agrees(*lexicon.score(text))
    assert agrees(*lexicon.score(" ".join(weights) + " " + text))
