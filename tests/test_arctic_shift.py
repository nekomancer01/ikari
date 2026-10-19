import pytest
import requests

from subsentiment.arctic_shift import BASE_URL, SETTLE_SECONDS, ArcticShiftClient, RateLimiter
from subsentiment.reddit import FetchError, Post

NOW = 1_792_000_000


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClock:
    """A clock that only moves when the limiter sleeps, so waits can be checked exactly."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s


def post(i, settled=True, **kw):
    created = NOW - i * 10
    d = {"id": f"p{i}", "subreddit": "Python", "title": f"Post {i}", "selftext": "", "author": "a",
         "score": 40 + i, "num_comments": 3, "upvote_ratio": 0.9, "created_utc": created,
         "retrieved_on": created + 20, "permalink": f"/r/Python/comments/p{i}/post_{i}/"}
    if settled:
        d["_meta"] = {"retrieved_2nd_on": created + SETTLE_SECONDS}
    d.update(kw)
    return d


def comment(cid, parent, t, body="nice", score=5, settled=True, **kw):
    d = {"id": cid, "link_id": "t3_p1", "parent_id": parent, "author": "b", "body": body,
         "score": score, "created_utc": t, "retrieved_on": t + 10,
         "permalink": f"/r/Python/comments/p1/x/{cid}/"}
    if settled:
        d["_meta"] = {"retrieved_2nd_on": t + SETTLE_SECONDS}
    d.update(kw)
    return d


def ok(items, headers=None):
    return FakeResponse(200, {"data": items}, headers)


def client(responses):
    clock = FakeClock()
    c = ArcticShiftClient(session=FakeSession(responses), limiter=RateLimiter(1.0, clock, clock.sleep))
    c.clock = clock
    return c


def a_post():
    return Post("p1", "Python", "t", "", "a", None, None, None, 1.0, "https://www.reddit.com/r/Python/comments/p1/", "")


# -- posts -------------------------------------------------------------------

def test_posts_search_the_date_range_newest_first():
    c = client([ok([post(1), post(2)]), ok([post(2)])])
    posts = c.fetch_posts("r/Python", after=NOW - 86400, before=NOW, limit=10)
    assert [p.id for p in posts] == ["p1", "p2"]
    url, params = c.session.calls[0]
    assert url == BASE_URL + "/api/posts/search"
    assert params == {"subreddit": "Python", "sort": "desc", "limit": 100, "after": NOW - 86400, "before": NOW}
    assert posts[0].permalink == "https://www.reddit.com/r/Python/comments/p1/post_1/"
    assert c.session.headers["User-Agent"].startswith("ikari/")


def test_open_ended_range_sends_no_before():
    c = client([ok([])])
    c.fetch_posts("python", after=NOW - 60)
    assert "before" not in c.session.calls[0][1]


def test_pagination_moves_before_back_and_drops_repeats():
    first = [post(i) for i in range(100)]
    second = [post(99)] + [post(i) for i in range(100, 130)]
    c = client([ok(first), ok(second), ok([post(129)])])
    posts = c.fetch_posts("python", limit=500)
    assert len(posts) == 130 and len({p.id for p in posts}) == 130
    assert c.session.calls[1][1]["before"] == NOW - 990 + 1
    assert c.session.calls[2][1]["before"] == NOW - 1290 + 1


def test_limit_is_honoured_without_extra_requests():
    c = client([ok([post(i) for i in range(100)])])
    assert len(c.fetch_posts("python", limit=30)) == 30
    assert len(c.session.calls) == 1


def test_settled_posts_keep_their_numbers_fresh_ones_are_unknown():
    fresh = post(1, settled=False, score=1, num_comments=1)
    late = post(2, settled=False, retrieved_on=NOW + SETTLE_SECONDS)  # first archived after it settled
    c = client([ok([fresh, late, post(3)]), ok([])])
    p1, p2, p3 = c.fetch_posts("python")
    assert (p1.score, p1.num_comments, p1.upvote_ratio) == (None, None, None)
    assert (p2.score, p2.num_comments) == (42, 3)
    assert (p3.score, p3.num_comments, p3.upvote_ratio) == (43, 3, 0.9)


def test_query_is_passed_through():
    c = client([ok([])])
    c.fetch_posts("python", query="asyncio")
    assert c.session.calls[0][1]["query"] == "asyncio"


def test_removed_posts_are_skipped_and_removed_bodies_blanked():
    c = client([ok([post(1, removed_by_category="moderator"), post(2, selftext="[removed]")]), ok([])])
    posts = c.fetch_posts("python")
    assert [p.id for p in posts] == ["p2"] and posts[0].selftext == ""


@pytest.mark.parametrize("name, message", [("  r/ ", "Enter a subreddit"), ("not a sub", "isn't a valid")])
def test_bad_subreddit_is_rejected_before_any_request(name, message):
    c = client([])
    with pytest.raises(FetchError, match=message):
        c.fetch_posts(name)
    assert c.session.calls == []


def test_should_stop_ends_early():
    c = client([ok([post(1)])])
    assert c.fetch_posts("python", should_stop=lambda: True) == []
    assert c.session.calls == []


# -- comments ----------------------------------------------------------------

def test_comments_use_the_post_id_and_rank_by_score():
    c = client([ok([
        comment("c1", "t3_p1", 1, score=2),
        comment("c2", "t1_c1", 2, score=9),
        comment("c3", "t1_c2", 3, body="[deleted]"),
        comment("c4", "t1_c3", 4, score=1),
        comment("c5", "t1_zz", 5, score=0),  # parent not in the archive
        comment("c6", "t3_p1", 6, settled=False),
    ]), ok([])])
    comments = c.fetch_comments(a_post(), limit=50)
    assert [x.id for x in comments] == ["c2", "c1", "c4", "c5", "c6"]  # unscored last
    assert {x.id: x.depth for x in comments} == {"c1": 0, "c2": 1, "c4": 3, "c5": 1, "c6": 0}
    assert comments[-1].score is None
    url, params = c.session.calls[0]
    assert url == BASE_URL + "/api/comments/search"
    assert params == {"link_id": "p1", "sort": "asc", "limit": 100}
    assert comments[0].permalink == "https://www.reddit.com/r/Python/comments/p1/x/c2/"


def test_comment_depth_from_the_archive_wins():
    c = client([ok([comment("c1", "t1_gone", 1, depth=4)]), ok([])])
    assert c.fetch_comments(a_post())[0].depth == 4


def test_comments_paginate_forward():
    first = [comment(f"c{i}", "t3_p1", i) for i in range(100)]
    c = client([ok(first), ok([comment("c99", "t3_p1", 99), comment("c100", "t3_p1", 100)]), ok([])])
    comments = c.fetch_comments(a_post(), limit=500)
    assert len(comments) == 101
    assert c.session.calls[1][1]["after"] == 98


# -- rate limiting and retries -------------------------------------------------

def test_requests_are_spaced_one_second_apart():
    c = client([ok([post(1)]), ok([]), ok([])])
    c.fetch_posts("python")
    c.fetch_comments(a_post())
    assert c.clock.sleeps == [1.0, 1.0]  # three requests, the first goes out at once


def test_429_waits_for_the_reset_header_then_succeeds():
    c = client([FakeResponse(429, {"error": "slow down"}, {"X-RateLimit-Reset": "7"}), ok([post(1)]), ok([])])
    assert len(c.fetch_posts("python")) == 1
    assert 7.0 in c.clock.sleeps


def test_429_without_a_header_backs_off_exponentially():
    c = client([FakeResponse(429), FakeResponse(429), ok([])])
    c.fetch_posts("python")
    assert c.clock.sleeps == [5.0, 10.0]


def test_running_out_of_requests_pauses_before_the_next_one():
    c = client([ok([post(1)], {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "30"}), ok([])])
    c.fetch_posts("python")
    assert c.clock.sleeps == [30.0]


def test_waits_are_capped():
    c = client([FakeResponse(429, headers={"X-RateLimit-Reset": "9999"}), ok([])])
    c.fetch_posts("python")
    assert c.clock.sleeps == [120.0]


def test_server_and_network_errors_retry_then_give_up():
    c = client([FakeResponse(503), requests.ConnectionError("reset"), FakeResponse(502), FakeResponse(500),
                FakeResponse(503)])
    with pytest.raises(FetchError, match="busy"):
        c.fetch_posts("python")
    assert len(c.session.calls) == 5


def test_network_error_on_the_last_attempt_is_raised():
    c = client([requests.Timeout("slow")] * 5)
    with pytest.raises(requests.Timeout):
        c.fetch_posts("python")


def test_query_timeout_retries_with_smaller_pages():
    c = client([FakeResponse(200, {"data": None, "error": "Query timed out"}), ok([post(1)]), ok([])])
    c.fetch_posts("python")
    assert [params["limit"] for _, params in c.session.calls] == [100, 50, 100]


def test_other_errors_are_reported():
    with pytest.raises(FetchError, match="HTTP 400: bad param"):
        client([FakeResponse(400, {"error": "bad param"})]).fetch_posts("python")
    with pytest.raises(FetchError, match="^Arctic Shift: unknown field$"):
        client([FakeResponse(200, {"data": None, "error": "unknown field"})]).fetch_posts("python")
