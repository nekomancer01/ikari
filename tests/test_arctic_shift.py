import pytest

from subsentiment.arctic_shift import BASE_URL, ArcticShiftClient
from subsentiment.reddit import Post, RedditClient, RedditError
from subsentiment.sources import make_client
from test_reddit import FakeResponse, FakeSession

NOW = 1_700_100_000


def post(i, **kw):
    d = {"id": f"p{i}", "subreddit": "Python", "title": f"Post {i}", "selftext": "", "author": "a",
         "score": i, "num_comments": 1, "created_utc": NOW - i * 10}
    d.update(kw)
    return d


def comment(cid, parent, t, body="nice", score=1):
    return {"id": cid, "link_id": "t3_p1", "parent_id": parent, "author": "b", "body": body,
            "score": score, "created_utc": t}


def ok(items):
    return FakeResponse(200, {"data": items})


def client(responses):
    return ArcticShiftClient(session=FakeSession(responses), sleep=lambda s: None, min_interval=0, now=lambda: NOW)


def a_post():
    return Post("p1", "Python", "t", "", "a", 1, 0.9, 3, 1.0, "https://www.reddit.com/r/Python/comments/p1/", "")


def test_posts_query_the_search_endpoint_newest_first():
    c = client([ok([post(1), post(2)])])
    posts = c.fetch_posts("r/Python", "new", limit=10)
    assert [p.id for p in posts] == ["p1", "p2"]
    (url, params), = c.session.calls
    assert url == BASE_URL + "/api/posts/search"
    assert params == {"subreddit": "Python", "sort": "desc", "limit": 100}
    assert posts[0].permalink == "https://www.reddit.com/r/Python/comments/p1/"


def test_pagination_moves_before_back_and_drops_repeats():
    first = [post(i) for i in range(100)]
    second = [post(99)] + [post(i) for i in range(100, 130)]
    c = client([ok(first), ok(second)])
    posts = c.fetch_posts("python", "new", limit=500)
    assert len(posts) == 130 and len({p.id for p in posts}) == 130
    assert c.session.calls[1][1]["before"] == NOW - 990 + 1


def test_limit_is_honoured():
    c = client([ok([post(i) for i in range(100)])])
    assert len(c.fetch_posts("python", limit=30)) == 30


def test_top_uses_time_window_and_ranks_by_score():
    c = client([ok([post(1), post(5), post(3)])])
    posts = c.fetch_posts("python", "top", "day", limit=10)
    assert [p.id for p in posts] == ["p5", "p3", "p1"]
    assert c.session.calls[0][1]["after"] == NOW - 86400


def test_all_time_and_listing_sorts_send_no_window():
    c = client([ok([]), ok([])])
    c.fetch_posts("python", "top", "all")
    c.fetch_posts("python", "hot", "day")
    assert all("after" not in params for _, params in c.session.calls)


def test_query_is_passed_through():
    c = client([ok([])])
    c.fetch_posts("python", "relevance", "week", query="asyncio")
    params = c.session.calls[0][1]
    assert params["query"] == "asyncio" and params["after"] == NOW - 7 * 86400


def test_removed_posts_are_skipped_and_removed_bodies_blanked():
    c = client([ok([post(1, removed_by_category="moderator"), post(2, selftext="[removed]")])])
    posts = c.fetch_posts("python", limit=10)
    assert [p.id for p in posts] == ["p2"] and posts[0].selftext == ""


def test_blank_subreddit_is_rejected():
    c = client([])
    with pytest.raises(RedditError):
        c.fetch_posts("  r/ ")


def test_should_stop_ends_early():
    c = client([ok([post(1)])])
    assert c.fetch_posts("python", should_stop=lambda: True) == []
    assert c.session.calls == []


def test_comments_get_depth_from_parents_and_skip_deleted():
    c = client([ok([
        comment("c1", "t3_p1", 1, score=2),
        comment("c2", "t1_c1", 2, score=9),
        comment("c3", "t1_c2", 3, body="[deleted]"),
        comment("c4", "t1_c3", 4, score=1),
        comment("c5", "t1_zz", 5, score=0),  # parent not in the archive
    ])])
    comments = c.fetch_comments(a_post(), limit=50)
    assert [x.id for x in comments] == ["c2", "c1", "c4", "c5"]  # top sort by score
    depth = {x.id: x.depth for x in comments}
    assert depth == {"c1": 0, "c2": 1, "c4": 3, "c5": 1}
    url, params = c.session.calls[0]
    assert url == BASE_URL + "/api/comments/search"
    assert params == {"link_id": "p1", "sort": "asc", "limit": 100}
    assert comments[0].permalink == "https://www.reddit.com/r/Python/comments/p1/_/c2/"


def test_comments_paginate_forward():
    first = [comment(f"c{i}", "t3_p1", i) for i in range(100)]
    c = client([ok(first), ok([comment("c99", "t3_p1", 99), comment("c100", "t3_p1", 100)])])
    comments = c.fetch_comments(a_post(), limit=500, sort="new")
    assert len(comments) == 101
    assert c.session.calls[1][1]["after"] == 98


def test_429_waits_for_reset_header_then_succeeds():
    waits = []
    c = ArcticShiftClient(session=FakeSession([FakeResponse(429, headers={"X-RateLimit-Reset": "7"}), ok([post(1)])]),
                          sleep=waits.append, min_interval=0)
    assert len(c.fetch_posts("python")) == 1
    assert 7.0 in waits


def test_error_payload_and_bad_status_raise_clear_errors():
    with pytest.raises(RedditError, match="Query timed out"):
        client([FakeResponse(200, {"data": None, "error": "Query timed out"})]).fetch_posts("python")
    with pytest.raises(RedditError, match="HTTP 400: bad param"):
        client([FakeResponse(400, {"error": "bad param"})]).fetch_posts("python")


def test_server_errors_retry_then_give_up():
    c = client([FakeResponse(503)] * 4)
    with pytest.raises(RedditError, match="busy"):
        c.fetch_posts("python")


def test_make_client_prefers_reddit_only_with_both_keys():
    assert isinstance(make_client({"client_id": "id", "client_secret": "s", "user_agent": "ua"}), RedditClient)
    assert isinstance(make_client({"client_id": "id", "client_secret": "", "user_agent": "ua"}), ArcticShiftClient)
    assert isinstance(make_client({}), ArcticShiftClient)
