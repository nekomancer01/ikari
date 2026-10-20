import threading
import time

import pytest
import requests

from subsentiment.arctic_shift import (
    BASE_URL, SETTLE_SECONDS, TOP_PAGES, ArcticShiftClient, QueryTimeout, RateLimiter, Stopped, quotas, slices, slot_count,
)
from subsentiment import arctic_shift
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

DAY = 86400
T0 = NOW - 7 * DAY


def at(t, i, **kw):
    """A settled post created at time t."""
    return post(i, created_utc=t, retrieved_on=t + 20, _meta={"retrieved_2nd_on": t + SETTLE_SECONDS}, **kw)


class ArchiveSession(FakeSession):
    """Answers /api/posts/search from a list of posts, honouring after/before/sort/limit like the API."""

    def __init__(self, items, page_cap=100):
        super().__init__([])
        self.items = sorted(items, key=lambda d: d["created_utc"])
        self.page_cap = page_cap

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        if url.endswith("/api/posts/ids"):
            ids = params["ids"].split(",")
            if len(ids) > 500:  # the API's own bounds
                return FakeResponse(400, {"error": "too many ids"})
            return ok(self._fields([d for d in self.items if d["id"] in ids], params))
        hits = [d for d in self.items if params["after"] < d["created_utc"] < params["before"]]
        limit = params["limit"]
        if limit != "auto" and not 1 <= limit <= 100:
            return FakeResponse(400, {"error": "limit must be between 1 and 100"})
        n = self.page_cap if limit == "auto" else limit
        return ok(self._fields(hits[:n], params))

    @staticmethod
    def _fields(items, params):
        """Like the API, ``fields`` keeps only the named fields, and drops _meta with the rest."""
        if "fields" not in params:
            return items
        keep = params["fields"].split(",")
        return [{k: d[k] for k in keep if k in d} for d in items]


def archive(items, **kw):
    clock = FakeClock()
    return ArcticShiftClient(session=ArchiveSession(items, **kw), limiter=RateLimiter(0.0, clock, clock.sleep))


def test_slices_and_quotas_split_evenly():
    assert slices(0, 100, 4) == [(0, 25), (25, 50), (50, 75), (75, 100)]
    assert quotas(10, 4) == [2, 3, 2, 3] and sum(quotas(1000, 7)) == 1000
    assert slot_count(0, 7 * DAY, 100, top=False) == 28  # four parts a day
    assert slot_count(0, 365 * DAY, 100, top=False) == 100  # never more parts than posts
    assert slot_count(0, 365 * DAY, 1000, top=False) == 365  # about one a day for long ranges
    assert slot_count(0, 7 * DAY, 100, top=True) == 7


def test_posts_are_spread_evenly_across_the_days():
    # A busy first day and quiet later days: newest-first paging would only ever see day one.
    items = [at(T0 + 60 + i * 43, i) for i in range(2000)]  # a post every 43 s on day one
    items += [at(T0 + d * DAY + 3600 * h, 10_000 + d * 100 + h) for d in range(1, 7) for h in range(24)]
    c = archive(items)
    posts = c.fetch_posts("python", after=T0, before=T0 + 7 * DAY, limit=70)
    assert len(posts) == 70
    per_day = [sum(1 for p in posts if T0 + d * DAY <= p.created_utc < T0 + (d + 1) * DAY) for d in range(7)]
    assert per_day == [10] * 7
    assert [p.created_utc for p in posts] == sorted(p.created_utc for p in posts)
    url, params = c.session.calls[0]
    assert url == BASE_URL + "/api/posts/search"
    assert params["subreddit"] == "python" and params["sort"] == "asc" and params["limit"] <= 100
    assert len(c.session.calls) == 28  # one small request per quarter-day, not thousands of posts
    assert c.session.headers["User-Agent"].startswith("ikari/")


def test_saved_posts_are_skipped_so_a_repeat_fetch_brings_new_ones():
    items = [at(T0 + 100 + i * 60, i) for i in range(200)]
    c = archive(items)
    first = c.fetch_posts("python", after=T0, before=T0 + DAY, limit=20)
    second = c.fetch_posts("python", after=T0, before=T0 + DAY, limit=20, skip={p.id for p in first})
    assert len(second) == 20 and not {p.id for p in first} & {p.id for p in second}


def test_a_slice_pages_forward_when_saved_posts_fill_the_first_page():
    items = [at(T0 + 100 + i, i) for i in range(300)]
    c = archive(items)
    skip = {f"p{i}" for i in range(150)}
    (got,) = [c.fetch_posts("python", after=T0, before=T0 + 3600, limit=1, skip=skip)]
    assert [p.id for p in got] == ["p150"]
    assert c.session.calls[1][1]["after"] == T0 + 100 + 99 - 1  # resumes a second before the last post seen


def test_quiet_slices_hand_their_share_to_busy_ones():
    items = [at(T0 + 100 + i * 60, i) for i in range(200)]  # all within the first 6 hours
    posts = archive(items).fetch_posts("python", after=T0, before=T0 + DAY, limit=20)
    assert [p.id for p in posts] == [f"p{i}" for i in range(20)]


def test_top_mode_ranks_settled_scores_within_each_day():
    items = [at(T0 + d * DAY + 600 * k, d * 100 + k, score=(k * 37) % 101) for d in range(3) for k in range(100)]
    c = archive(items)
    posts = c.fetch_posts("python", after=T0, before=T0 + 3 * DAY, limit=6, top=True)
    assert len(posts) == 6
    for d in range(3):
        day = sorted((x for x in items if T0 + d * DAY <= x["created_utc"] < T0 + (d + 1) * DAY),
                     key=lambda x: x["score"], reverse=True)
        assert {p.id for p in posts if T0 + d * DAY <= p.created_utc < T0 + (d + 1) * DAY} == \
            {x["id"] for x in day[:2]}
    searches = [params for url, params in c.session.calls if url.endswith("/search")]
    assert {params["limit"] for params in searches} == {100}
    assert {params["fields"] for params in searches} == {"id,created_utc,score,num_comments"}  # light copies
    assert [url.rsplit("/", 1)[1] for url, _ in c.session.calls][-1] == "ids"  # then the winners, in one batch


def test_top_mode_puts_unscored_posts_last():
    items = [at(T0 + 10, 1, score=5), post(2, settled=False, created_utc=T0 + 20, retrieved_on=T0 + 30, score=1)]
    posts = archive(items).fetch_posts("python", after=T0, before=T0 + DAY, limit=1, top=True)
    assert [p.id for p in posts] == ["p1"]


def test_settled_posts_keep_their_numbers_fresh_ones_are_unknown():
    fresh = post(1, settled=False, score=1, num_comments=1)
    late = post(2, settled=False, retrieved_on=NOW + SETTLE_SECONDS)  # first archived after it settled
    c = archive([fresh, late, post(3)])
    by_id = {p.id: p for p in c.fetch_posts("python", after=NOW - DAY, before=NOW + 1)}
    assert (by_id["p1"].score, by_id["p1"].num_comments, by_id["p1"].upvote_ratio) == (None, None, None)
    assert (by_id["p2"].score, by_id["p2"].num_comments) == (42, 3)
    assert (by_id["p3"].score, by_id["p3"].num_comments, by_id["p3"].upvote_ratio) == (43, 3, 0.9)
    assert by_id["p3"].permalink == "https://www.reddit.com/r/Python/comments/p3/post_3/"


def test_query_is_passed_through():
    c = archive([])
    c.fetch_posts("python", after=T0, before=T0 + DAY, query="asyncio")
    assert c.session.calls[0][1]["query"] == "asyncio"


def test_removed_posts_are_skipped_and_removed_bodies_blanked():
    items = [post(1, removed_by_category="moderator"), post(2, selftext="[removed]"),
             post(3, _meta={"retrieved_2nd_on": NOW, "removal_type": "moderator", "was_deleted_later": True})]
    c = archive(items)
    posts = c.fetch_posts("python", after=NOW - DAY, before=NOW + 1)
    assert [p.id for p in posts] == ["p2"] and posts[0].selftext == ""
    assert set(c.removed) == {"p1", "p3"}  # noted, so coverage can count them as checked


def test_image_posts_are_skipped_without_using_up_the_limit_and_noted():
    items = [post(1, url="https://i.redd.it/a.jpeg", post_hint="image"), post(2, url="https://www.reddit.com/gallery/g", is_gallery=True),
             post(3, url="https://i.redd.it/b.jpeg", selftext="my desk, details below"), post(4), post(5)]
    c = archive(items)
    posts = c.fetch_posts("python", after=NOW - DAY, before=NOW + 1, limit=3)
    assert sorted(p.id for p in posts) == ["p3", "p4", "p5"]  # the two image posts didn't count toward 3
    assert sorted(p.id for p in c.take_images()) == ["p1", "p2"] and c.take_images() == []
    c.fetch_posts("python", after=NOW - DAY, before=NOW + 1, limit=10, skip={"p1", "p2"})
    assert c.take_images() == []  # already saved image posts aren't noted again


def test_posts_by_id_are_fetched_in_batches_of_500():
    items = [at(T0 + i, i) for i in range(1200)]
    c = archive(items)
    got = c.fetch_posts_by_ids([f"p{i}" for i in range(1200)] + ["p1"])
    assert len(got) == 1200
    assert [len(params["ids"].split(",")) for _, params in c.session.calls] == [500, 500, 200]
    assert all(url == BASE_URL + "/api/posts/ids" for url, _ in c.session.calls)


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

def node(d, replies=()):
    return {"kind": "t1", "data": dict(d, replies={"kind": "Listing", "data": {"children": list(replies)}}
                                       if replies else "")}


def test_comments_come_from_the_tree_in_one_request_ranked_by_score():
    tree = [
        node(comment("c1", "t3_p1", 1, score=2, depth=0), [
            node(comment("c2", "t1_c1", 2, score=9, depth=1), [
                node(comment("c3", "t1_c2", 3, body="[deleted]", depth=2), [
                    node(comment("c4", "t1_c3", 4, score=1, depth=3))])])]),
        node(comment("c6", "t3_p1", 6, settled=False, depth=0)),
        {"kind": "more", "data": {"count": 15, "children": ["x", "y"], "depth": 0}},
    ]
    c = client([ok(tree)])
    comments = c.fetch_comments(a_post(), limit=50)
    assert [x.id for x in comments] == ["c2", "c1", "c4", "c6"]  # unscored last
    assert {x.id: x.depth for x in comments} == {"c1": 0, "c2": 1, "c4": 3, "c6": 0}
    assert comments[-1].score is None
    url, params = c.session.calls[0]
    assert url == BASE_URL + "/api/comments/tree"
    assert params == {"link_id": "p1", "limit": 150}  # the post's comment count isn't known yet
    assert len(c.session.calls) == 1
    assert comments[0].permalink == "https://www.reddit.com/r/Python/comments/p1/x/c2/"


def test_depth_is_worked_out_when_the_archive_leaves_it_out():
    tree = [node(comment("c1", "t3_p1", 1), [node(comment("c2", "t1_c1", 2))])]
    assert {x.id: x.depth for x in client([ok(tree)]).fetch_comments(a_post())} == {"c1": 0, "c2": 1}


def test_big_threads_ask_for_a_bigger_tree_and_keep_the_top_ones():
    p = Post("p1", "Python", "t", "", "a", 10, 0.9, 5000, 1.0, "u", "")
    tree = [node(comment(f"c{i}", "t3_p1", i, score=i, depth=0)) for i in range(30)]
    c = client([ok(tree)])
    got = c.fetch_comments(p, limit=5)
    assert c.session.calls[0][1]["limit"] == 300
    assert [x.id for x in got] == ["c29", "c28", "c27", "c26", "c25"]


# -- rate limiting and retries -------------------------------------------------

SEARCH = ("/api/posts/search", {"subreddit": "Python", "limit": 100})


def test_requests_are_spaced_one_second_apart_across_posts_and_comments():
    c = client([ok([post(1)]), ok([]), ok([])])
    c.get(*SEARCH)
    c.fetch_posts_by_ids(["p1"])
    c.fetch_comments(a_post())
    assert c.clock.sleeps == [1.0, 1.0]  # three requests, the first goes out at once


def test_429_waits_for_the_reset_header_then_succeeds():
    c = client([FakeResponse(429, {"error": "slow down"}, {"X-RateLimit-Reset": "7"}), ok([post(1)])])
    assert len(c.get(*SEARCH)) == 1
    assert 7.0 in c.clock.sleeps


def test_429_with_a_reset_time_waits_until_then():
    c = client([FakeResponse(429, headers={"X-RateLimit-Reset-At": "5025"}), ok([])])
    c.limiter._wall = lambda: 5000.0
    c.get(*SEARCH)
    assert c.clock.sleeps == [25.0]


def test_429_without_a_header_backs_off_and_slows_later_requests():
    c = client([FakeResponse(429), FakeResponse(429), ok([]), ok([])])
    c.get(*SEARCH)
    assert c.clock.sleeps == [5.0, 10.0]
    assert c.limiter.interval == 4.0 * 0.8  # doubled twice, eased once by the success
    c.get(*SEARCH)
    assert c.clock.sleeps[-1] == 4.0  # the next request waits 4 s instead of 1


def test_long_waits_are_announced():
    c = client([FakeResponse(429, headers={"X-RateLimit-Reset": "30"}), ok([])])
    told = []
    c.limiter.on_wait = lambda secs, why: told.append((round(secs), why))
    c.get(*SEARCH)
    assert told == [(30, "Arctic Shift asked ikari to slow down"), (0, "")]  # and when the wait is over


def test_running_out_of_requests_pauses_before_the_next_one():
    c = client([ok([post(1)], {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "30"}), ok([])])
    c.get(*SEARCH)
    c.get(*SEARCH)
    assert c.clock.sleeps == [30.0]


def test_waits_are_capped():
    c = client([FakeResponse(429, headers={"X-RateLimit-Reset": "9999"}), ok([])])
    c.get(*SEARCH)
    assert c.clock.sleeps == [120.0]


def test_server_and_network_errors_retry_then_give_up():
    c = client([FakeResponse(503), requests.ConnectionError("reset"), FakeResponse(502), FakeResponse(500),
                FakeResponse(503), FakeResponse(503), FakeResponse(504), FakeResponse(503)])
    with pytest.raises(FetchError, match="busy"):
        c.get(*SEARCH)
    assert len(c.session.calls) == 8


def test_network_error_on_the_last_attempt_is_raised():
    c = client([requests.Timeout("slow")] * 8)
    with pytest.raises(requests.Timeout):
        c.get(*SEARCH)


def test_query_timeouts_back_off_with_smaller_pages_then_give_up():
    slow = FakeResponse(422, {"error": "Timeout. Maybe slow down a bit"})
    c = client([slow, ok([post(1)])])
    assert len(c.get("/api/posts/search", {"limit": 100})) == 1
    assert [params["limit"] for _, params in c.session.calls] == [100, 50]
    assert c.clock.sleeps == [2.0]  # a short pause: timeouts are about query size, not request rate

    timed_out = FakeResponse(200, {"data": None, "error": "Query timed out"})
    c = client([timed_out, slow, slow])
    with pytest.raises(QueryTimeout, match="shorter date range"):
        c.get("/api/posts/search", {"limit": 20})
    assert [params["limit"] for _, params in c.session.calls] == [20, 10, 10]


class SlowArchive(ArchiveSession):
    """Times out on any search window longer than ``max_window`` seconds."""

    def __init__(self, items, max_window):
        super().__init__(items)
        self.max_window = max_window

    def get(self, url, params=None, timeout=None):
        if params["before"] - params["after"] > self.max_window:
            self.calls.append((url, dict(params)))
            return FakeResponse(422, {"error": "Timeout. Maybe slow down a bit"})
        return super().get(url, params, timeout)


def test_a_timing_out_window_is_narrowed_and_the_rest_still_read():
    items = [at(T0 + 100 + i * 600, i) for i in range(30)]  # one post every 10 minutes for 5 hours
    clock = FakeClock()
    session = SlowArchive(items, max_window=1800)
    c = ArcticShiftClient(session=session, limiter=RateLimiter(0.0, clock, clock.sleep))
    posts = c.fetch_posts("python", after=T0, before=T0 + 6 * 3600, limit=30)  # four 90-minute slices
    assert [p.id for p in posts] == [f"p{i}" for i in range(30)]
    widths = {params["before"] - params["after"] for _, params in session.calls}
    assert max(widths) == 5401 and min(widths) <= 1800  # gave up on 90 minutes (from a second early), read narrower windows


class StuckArchive(ArchiveSession):
    """Times out on any search window holding ``stuck``, however narrow."""

    def __init__(self, items, stuck):
        super().__init__(items)
        self.stuck = stuck

    def get(self, url, params=None, timeout=None):
        if url.endswith("/api/posts/search") and params["after"] < self.stuck < params["before"]:
            self.calls.append((url, dict(params)))
            return FakeResponse(422, {"error": "Timeout. Maybe slow down a bit"})
        return super().get(url, params, timeout)


def test_a_window_that_times_out_however_narrow_leaves_the_rest_of_the_range_read():
    # Live on Sat 17 Oct, one narrowed window kept timing out and the whole Download all failed with
    # an error box at 69%. The rest of the day can still be read; the day just can't be called whole.
    items = [at(T0 + 100 + i * 600, i) for i in range(140)]  # a post every 10 minutes for a day
    clock = FakeClock()
    c = ArcticShiftClient(session=StuckArchive(items, stuck=T0 + 3 * 3600 + 50),
                          limiter=RateLimiter(0.0, clock, clock.sleep))
    posts = c.fetch_posts("python", after=T0, before=T0 + DAY, limit=10000, exhaust=True)
    got = {p.created_utc for p in posts}
    assert all(T0 + h * 3600 + 100 in got for h in (0, 2, 7, 12, 20))  # every other part of the day was read
    assert len(posts) >= 100 and not c.exhausted and c.timed_out
    c.fetch_posts("python", after=T0 + 12 * 3600, before=T0 + DAY, limit=10000, exhaust=True)
    assert c.exhausted and not c.timed_out  # a fetch that doesn't touch it is unaffected


def test_other_errors_are_reported():
    with pytest.raises(FetchError, match="HTTP 400: bad param"):
        client([FakeResponse(400, {"error": "bad param"})]).get(*SEARCH)
    with pytest.raises(FetchError, match="^Arctic Shift: unknown field$"):
        client([FakeResponse(200, {"data": None, "error": "unknown field"})]).get(*SEARCH)


def test_default_spacing_is_a_few_requests_a_second():
    assert RateLimiter().interval == pytest.approx(0.4)


def test_top_picks_that_turn_out_removed_are_replaced_by_the_next_best():
    items = [at(T0 + 100 + k, k, score=100 - k) for k in range(10)]
    items[0]["_meta"]["removal_type"] = "moderator"  # the best one was removed; only the full copy says so
    posts = archive(items).fetch_posts("python", after=T0, before=T0 + DAY, limit=3, top=True)
    assert sorted(p.id for p in posts) == ["p1", "p2", "p3"]


# -- coverage: day counts and whole threads ------------------------------------

D0 = 1_791_936_000  # a UTC midnight


def test_day_counts_read_each_bucket_format_and_fill_empty_days():
    buckets = [{"created_utc": "2026-10-14T00:00:00.000Z", "count": 12},  # an ISO date
               {"created_utc": (D0 + DAY) * 1000, "count": 3},  # milliseconds
               {"created_utc": D0 + 3 * DAY, "doc_count": 7}]  # seconds
    c = client([ok(buckets)])
    counts = c.day_counts("r/Python", D0 + 3600, D0 + 4 * DAY - 10)
    assert counts == {D0: 12, D0 + DAY: 3, D0 + 2 * DAY: 0, D0 + 3 * DAY: 7}
    url, params = c.session.calls[0]
    assert url == BASE_URL + "/api/posts/search/aggregate"
    assert params == {"subreddit": "Python", "aggregate": "created_utc", "frequency": "day",
                      "after": D0, "before": D0 + 4 * DAY, "limit": 5}


def test_a_timing_out_count_is_split_and_a_day_that_still_times_out_is_left_out():
    slow = FakeResponse(422, {"error": "Timeout. Maybe slow down a bit"})
    # two days: the whole range times out three times, then day one works and day two keeps timing out,
    # including the second go it gets at the end; it's left out (unknown), never counted as 0
    c = client([slow] * 3 + [ok([{"created_utc": D0, "count": 4}])] + [slow] * 6)
    assert c.day_counts("Python", D0, D0 + 2 * DAY) == {D0: 4}
    assert [p["limit"] for _, p in c.session.calls] == [3, 3, 3] + [2] * 7  # never shrunk like a page
    assert [p["after"] for _, p in c.session.calls[-6:]] == [D0 + DAY] * 6


def test_a_day_whose_count_timed_out_gets_a_second_go():
    slow = FakeResponse(422, {"error": "Timeout. Maybe slow down a bit"})
    c = client([slow] * 3 + [ok([{"created_utc": D0 - 7200, "count": 4}])])
    assert c.day_counts("Python", D0, D0 + DAY) == {D0: 4}


def test_day_buckets_keyed_two_hours_before_utc_midnight_count_for_that_utc_day():
    # The archive keys a UTC day's bucket at 22:00Z the day before (its own midnight, UTC+2), and the
    # hour before the clocks change, UTC+1: 23:00Z. Each still belongs to the UTC day that follows.
    buckets = [{"created_utc": "2026-10-13T22:00:00.000Z", "count": 12},
               {"created_utc": (D0 + DAY - 7200) * 1000, "count": 3},
               {"created_utc": D0 + 3 * DAY - 3600, "count": 7},
               {"created_utc": D0 + 4 * DAY - 7200, "count": 9}]  # the day after the range
    counts = client([ok(buckets)]).day_counts("Python", D0, D0 + 4 * DAY)
    assert counts == {D0: 12, D0 + DAY: 3, D0 + 2 * DAY: 0, D0 + 3 * DAY: 7}


def test_all_comments_ask_for_the_whole_thread_and_say_whether_it_is_complete():
    p = Post("p1", "Python", "t", "", "a", 10, 0.9, 1200, 1.0, "u", "")
    tree = [node(comment(f"c{i}", "t3_p1", i)) for i in range(5)]
    c = client([ok(tree)])
    comments, whole = c.fetch_all_comments(p)
    assert len(comments) == 5 and whole
    assert c.session.calls[0][1] == {"link_id": "p1", "limit": 1250}

    collapsed = tree + [{"kind": "more", "data": {"count": 4, "children": ["x1", "x2"]}}]
    assert client([ok(collapsed)]).fetch_all_comments(p)[1] is False

    small = Post("p1", "Python", "t", "", "a", 10, 0.9, 0, 1.0, "u", "")  # asks for at least a page
    full = [node(comment(f"c{i}", "t3_p1", i)) for i in range(100)]
    assert client([ok(full)]).fetch_all_comments(small)[1] is False  # a full page may have been cut short


class BusyArchive(ArchiveSession):
    """Times out on any page bigger than ``max_limit``, so the client retries with smaller pages."""

    def __init__(self, items, max_limit):
        super().__init__(items)
        self.max_limit = max_limit

    def get(self, url, params=None, timeout=None):
        if url.endswith("/api/posts/search") and params["limit"] > self.max_limit:
            self.calls.append((url, dict(params)))
            return FakeResponse(422, {"error": "Timeout. Maybe slow down a bit"})
        return super().get(url, params, timeout)


def test_a_page_shrunk_after_a_timeout_doesnt_end_the_slice_early():
    # Every slice starts with 100 saved posts, so finding new ones needs full pages, which time out.
    items = [at(T0 + d * DAY + 60 * k, d * 1000 + k) for d in range(7) for k in range(300)]
    skip = {d["id"] for d in items if int(d["id"][1:]) % 1000 < 100}
    clock = FakeClock()
    c = ArcticShiftClient(session=BusyArchive(items, max_limit=50), limiter=RateLimiter(0.0, clock, clock.sleep))
    posts = c.fetch_posts("python", after=T0, before=T0 + 7 * DAY, limit=28, skip=skip)
    assert len(posts) == 28 and not skip & {p.id for p in posts}
    per_day = [sum(1 for p in posts if T0 + d * DAY <= p.created_utc < T0 + (d + 1) * DAY) for d in range(7)]
    assert per_day == [4] * 7
    timeouts = [p for _, p in c.session.calls if p["limit"] > 50]
    assert len(timeouts) == 1  # the smaller page that worked is kept, not retried at full size each time


def busy_day(n, removed_every=5):
    """A day holding n posts spread evenly, every ``removed_every``-th one removed on Reddit."""
    step = DAY / n
    return [at(int(T0 + 1 + k * step), k, **({"removed_by_category": "deleted"} if k % removed_every == 0 else {}))
            for k in range(n)]


def test_reading_a_whole_busy_day_goes_on_until_every_slice_runs_out():
    # Sat 17 Oct on r/AskReddit: 4,288 posts, about a fifth removed, and the archive kept timing out on
    # full pages. "Download all" stopped at 2,639 of them and still said the day was complete.
    items = busy_day(4000)
    usable = {d["id"] for d in items if "removed_by_category" not in d}
    clock = FakeClock()
    sample = ArcticShiftClient(session=BusyArchive(items, max_limit=25), limiter=RateLimiter(0.0, clock, clock.sleep))
    short = sample.fetch_posts("python", after=T0, before=T0 + DAY, limit=10000)
    assert len(short) < len(usable) and not sample.exhausted  # a few rounds only: it can't say it got them all

    c = ArcticShiftClient(session=BusyArchive(items, max_limit=25), limiter=RateLimiter(0.0, clock, clock.sleep))
    posts = c.fetch_posts("python", after=T0, before=T0 + DAY, limit=10000, exhaust=True)
    assert {p.id for p in posts} == usable and c.exhausted
    assert len(c.removed) == len(items) - len(usable)


def test_a_whole_day_read_to_the_limit_or_stopped_is_not_called_exhausted():
    items = busy_day(400)
    c = archive(items)
    assert len(c.fetch_posts("python", after=T0, before=T0 + DAY, limit=100, exhaust=True)) == 100
    assert not c.exhausted  # stopped at the limit with posts left
    calls = []
    stop = lambda: len(calls) > 3 or calls.append(1)  # noqa: E731
    c.fetch_posts("python", after=T0, before=T0 + DAY, limit=10000, exhaust=True, should_stop=stop)
    assert not c.exhausted
    c.fetch_posts("python", after=T0, before=T0 + DAY, limit=10000, exhaust=True)
    assert c.exhausted
    c.fetch_posts("python", after=T0, before=T0 + DAY, limit=5, top=True)
    assert not c.exhausted  # top mode picks, it never reads everything


def test_a_post_right_on_a_slice_edge_is_found():
    items = [at(T0 + 6 * 3600, 1), at(T0 + 6 * 3600 + 5, 2)]  # the first sits exactly where slice two starts
    posts = archive(items).fetch_posts("python", after=T0, before=T0 + DAY, limit=4)
    assert [p.id for p in posts] == ["p1", "p2"]


class SlowServer(ArchiveSession):
    """Takes ``delay`` real seconds to answer, and records how many requests were in flight at once."""

    def __init__(self, items, delay):
        super().__init__(items)
        self.delay, self.busy, self.most = delay, 0, 0
        self._lock = threading.Lock()

    def get(self, url, params=None, timeout=None):
        with self._lock:
            self.busy += 1
            self.most = max(self.most, self.busy)
        time.sleep(self.delay)
        try:
            return super().get(url, params, timeout)
        finally:
            with self._lock:
                self.busy -= 1


def test_slices_are_read_a_few_at_once_with_the_same_result():
    items = [at(T0 + d * DAY + 3600 * h, d * 100 + h) for d in range(7) for h in range(24)]
    got = {}
    for workers in (1, 4):
        session = SlowServer(items, delay=0.1)
        c = ArcticShiftClient(session=session, limiter=RateLimiter(0.0), workers=workers)
        start = time.perf_counter()
        seen_on = set()
        posts = c.fetch_posts("python", after=T0, before=T0 + 7 * DAY, limit=28,
                              on_posts=lambda batch: seen_on.add(threading.get_ident()))
        got[workers] = ([p.id for p in posts], time.perf_counter() - start, session.most)
        assert seen_on == {threading.get_ident()}  # results are handed over on the calling thread
    assert got[1][0] == got[4][0]
    assert got[4][2] == 4 and got[1][2] == 1
    assert got[4][1] < got[1][1] / 2.5  # 28 slices of 100 ms: about 2.8 s one at a time, 0.7 s four at once


def test_requests_still_start_no_faster_than_the_limiter_allows():
    items = [at(T0 + d * DAY + 3600 * h, d * 100 + h) for d in range(7) for h in range(24)]
    starts = []
    limiter = RateLimiter(0.02)
    real_wait, grab = limiter.wait, threading.Lock()

    def wait(should_stop=None):
        # When the limiter let each request go, by its own clock. Timing anything after that also counts how
        # late the OS ran each worker thread, which on a busy machine (the suite runs in parallel) bunches
        # two together. The lock keeps another grant from moving _next before it's read.
        with grab:
            real_wait(should_stop)
            starts.append(limiter._next - limiter.interval)

    limiter.wait = wait
    ArcticShiftClient(session=SlowServer(items, delay=0.05), limiter=limiter, workers=4).fetch_posts(
        "python", after=T0, before=T0 + 7 * DAY, limit=28)
    assert len(starts) >= 28
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    # The limiter's clock (time.monotonic) only ticks every ~16 ms on Windows before Python 3.13, so there a
    # gap can read up to one tick short of the interval; nothing ever starts together.
    tick = time.get_clock_info("monotonic").resolution
    assert min(gaps) >= 0.018 - tick


def test_an_error_in_one_slice_stops_the_fetch():
    class Broken(ArchiveSession):
        def get(self, url, params=None, timeout=None):
            if params["after"] > T0 + 3 * DAY:
                return FakeResponse(400, {"error": "bad param"})
            return super().get(url, params, timeout)
    items = [at(T0 + d * DAY + 3600 * h, d * 100 + h) for d in range(7) for h in range(24)]
    with pytest.raises(FetchError, match="bad param"):
        archive([]).__class__(session=Broken(items), limiter=RateLimiter(0.0)).fetch_posts(
            "python", after=T0, before=T0 + 7 * DAY, limit=28)


def test_top_posts_show_as_each_day_is_ranked():
    items = [at(T0 + d * DAY + 600 * k, d * 100 + k, score=(k * 37) % 101) for d in range(7) for k in range(100)]
    batches, lines = [], []
    posts = archive(items).fetch_posts("python", after=T0, before=T0 + 7 * DAY, limit=14, top=True,
                                       on_posts=batches.append, progress=lines.append)
    assert len(batches) == 7 and sum(map(len, batches)) == 14 == len(posts)
    assert all(p.title for b in batches for p in b)  # full posts, not light copies
    assert lines[0].endswith(", 2 so far")  # the count goes up as days are ranked, not "0 so far" throughout


def test_stopping_top_mode_keeps_the_days_already_ranked():
    items = [at(T0 + d * DAY + 600 * k, d * 100 + k, score=(k * 37) % 101) for d in range(7) for k in range(100)]
    c = archive(items)
    c.workers = 1
    stop = lambda: sum(1 for u, _ in c.session.calls if u.endswith("/api/posts/search")) >= 3 * TOP_PAGES  # noqa: E731
    posts = c.fetch_posts("python", after=T0, before=T0 + 7 * DAY, limit=14, top=True, should_stop=stop)
    assert len(posts) == 6 and all(p.title for p in posts)  # three days' picks, downloaded despite the Stop


def test_concurrent_requests_stay_within_the_servers_remaining_budget():
    # A server allowing 3 requests per 0.3 s window, and 4 threads asking as fast as the limiter lets them.
    lock, window, used, refused = threading.Lock(), [time.perf_counter()], [0], []

    def answer():
        with lock:
            now = time.perf_counter()
            if now - window[0] >= 0.3:
                window[0], used[0] = now, 0
            used[0] += 1
            if used[0] > 3:
                refused.append(now)
            return {"X-RateLimit-Remaining": str(3 - used[0]),
                    "X-RateLimit-Reset": f"{0.3 - (now - window[0]) + 0.01:.3f}"}

    limiter = RateLimiter(0.0)

    def worker():
        for _ in range(6):
            limiter.wait()
            time.sleep(0.02)  # the request is on the wire
            limiter.observe(answer())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert refused == [] and not any(t.is_alive() for t in threads)


def test_a_request_that_never_got_an_answer_frees_its_slot():
    limiter = RateLimiter(0.0)
    limiter.wait()
    limiter.done()
    finished = threading.Event()
    threading.Thread(target=lambda: (limiter.wait(), finished.set()), daemon=True).start()
    assert finished.wait(0.5)


def test_stop_cuts_short_the_retries_and_waits_of_requests_in_flight():
    # Every page times out, so each request backs off 2 s, then 4 s, then the window is halved and so on.
    class AlwaysSlow(ArchiveSession):
        def get(self, url, params=None, timeout=None):
            time.sleep(0.02)
            return FakeResponse(422, {"error": "Timeout. Maybe slow down a bit"})

    flag = threading.Event()
    c = ArcticShiftClient(session=AlwaysSlow([]), limiter=RateLimiter(0.0), should_stop=flag.is_set)
    threading.Timer(0.3, flag.set).start()
    start = time.perf_counter()
    assert c.fetch_posts("python", after=T0, before=T0 + DAY, limit=40, should_stop=flag.is_set) == []
    assert time.perf_counter() - start < 1.0


def test_stop_keeps_what_finished_slices_found():
    items = [at(T0 + 3600 * h, h) for h in range(24)]
    session = SlowServer(items, delay=0.05)
    flag = threading.Event()
    c = ArcticShiftClient(session=session, limiter=RateLimiter(0.0), workers=1, should_stop=flag.is_set)
    got = []

    def on_posts(batch):
        got.extend(batch)
        flag.set()  # Stop right after the first slice is in

    posts = c.fetch_posts("python", after=T0, before=T0 + DAY, limit=24, should_stop=flag.is_set,
                          on_posts=on_posts)
    assert posts == got and 0 < len(posts) < 24


def test_a_long_wait_ends_as_soon_as_stop_is_pressed():
    limiter = RateLimiter(0.0)
    limiter.hold(30)
    flag = threading.Event()
    threading.Timer(0.2, flag.set).start()
    start = time.perf_counter()
    with pytest.raises(Stopped):
        limiter.wait(flag.is_set)
    assert time.perf_counter() - start < 1.0


def test_each_answer_is_parsed_once():
    """A whole comment tree can be megabytes of JSON; reading it twice doubled the time spent on it."""
    class Counting(FakeResponse):
        parsed = 0

        def json(self):
            Counting.parsed += 1
            return super().json()

    session = FakeSession([Counting(200, {"data": [{"id": "x"}]})])
    client = ArcticShiftClient(session=session, limiter=RateLimiter(interval=0, sleep=lambda s: None))
    assert client.get("/api/comments/tree", {"link_id": "abc"}) == [{"id": "x"}]
    assert Counting.parsed == 1


def test_an_ok_answer_that_isnt_json_is_retried_and_fails_only_if_it_keeps_coming():
    # A proxy's or CDN's error page with HTTP 200, or a body cut short: it used to fail the whole fetch on
    # the first one, while a 503 is waited out and retried.
    session = FakeSession([FakeResponse(200, None)] * arctic_shift.GARBLED_TRIES)
    client = ArcticShiftClient(session=session, limiter=RateLimiter(interval=0, sleep=lambda s: None))
    with pytest.raises(FetchError, match="couldn't be read"):
        client.get("/api/posts/search", {"subreddit": "Python"})
    assert len(session.calls) == arctic_shift.GARBLED_TRIES


@pytest.mark.parametrize("bad", [FakeResponse(200, None), FakeResponse(200, ["not", "an", "object"]),
                                 requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead")])
def test_an_answer_that_cant_be_read_is_retried(bad):
    session = FakeSession([bad, FakeResponse(200, {"data": [{"id": "x"}, None, "junk"]})])
    client = ArcticShiftClient(session=session, limiter=RateLimiter(interval=0, sleep=lambda s: None))
    assert client.get("/api/posts/search", {"subreddit": "Python"}) == [{"id": "x"}]  # junk records skipped


def test_a_slow_request_still_out_holds_back_the_next_while_the_budget_is_unknown():
    # Only requests of a stopped job stop counting: a live slow one still keeps requests to one at a time.
    limiter = RateLimiter(0.0)
    limiter.wait()  # no answer has come yet, so the budget is unknown
    sent = threading.Event()
    threading.Thread(target=lambda: (limiter.wait(), sent.set()), daemon=True).start()
    assert not sent.wait(0.5)
    stopped = threading.Event()
    other = RateLimiter(0.0)
    other.wait(stopped.is_set)
    stopped.set()  # its job was stopped: nobody waits for that answer any more
    assert other.wait() is not None
