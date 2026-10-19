import pytest

from subsentiment.reddit import RedditClient, RedditError, flatten_comments


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)

    def post(self, url, auth=None, data=None, timeout=None):
        self.calls.append((url, data))
        return self.responses.pop(0)


def post(i, sub="Python"):
    return {"kind": "t3", "data": {"id": f"p{i}", "subreddit": sub, "title": f"Post {i}", "selftext": "",
                                   "author": "a", "score": i, "num_comments": 2, "created_utc": 1_700_000_000 + i,
                                   "permalink": f"/r/{sub}/comments/p{i}/"}}


def listing(children, after=None):
    return {"kind": "Listing", "data": {"children": children, "after": after}}


def client(responses, **kw):
    return RedditClient(session=FakeSession(responses), sleep=lambda s: None, min_interval=0, **kw)


def test_paginates_until_limit():
    c = client([FakeResponse(200, listing([post(i) for i in range(100)], after="t3_p99")),
                FakeResponse(200, listing([post(i) for i in range(100, 150)], after="t3_p149"))])
    posts = c.fetch_posts("r/Python", "new", limit=150)
    assert len(posts) == 150
    (url1, p1), (url2, p2) = c.session.calls
    assert url1 == "https://www.reddit.com/r/Python/new.json"
    assert p1["limit"] == 100 and p2 == {"limit": 50, "after": "t3_p99", "raw_json": 1}


def test_stops_when_listing_runs_out():
    c = client([FakeResponse(200, listing([post(1), post(2)], after=None))])
    assert [p.id for p in c.fetch_posts("python", limit=100)] == ["p1", "p2"]


def test_search_uses_search_endpoint():
    c = client([FakeResponse(200, listing([post(1)]))])
    c.fetch_posts("python", sort="rising", query="asyncio", time_filter="month")
    url, params = c.session.calls[0]
    assert url.endswith("/r/python/search.json")
    assert params["q"] == "asyncio" and params["restrict_sr"] == 1
    assert params["sort"] == "relevance" and params["t"] == "month"


def test_oauth_gets_token_and_uses_oauth_host():
    c = client([FakeResponse(200, {"access_token": "tok", "expires_in": 3600}),
                FakeResponse(200, listing([post(1)]))], client_id="id", client_secret="secret")
    c.fetch_posts("python", limit=1)
    assert c.session.calls[0][0] == "https://www.reddit.com/api/v1/access_token"
    assert c.session.calls[1][0] == "https://oauth.reddit.com/r/python/hot"


def test_retries_on_429_then_succeeds():
    c = client([FakeResponse(429, headers={"Retry-After": "1"}), FakeResponse(200, listing([post(1)]))])
    assert len(c.fetch_posts("python", limit=1)) == 1


def test_403_explains_anonymous_blocking():
    c = client([FakeResponse(403)])
    with pytest.raises(RedditError, match="API credentials"):
        c.fetch_posts("python")


def test_flatten_comments_walks_replies_and_skips_more_and_deleted():
    tree = [
        {"kind": "t1", "data": {"id": "c1", "body": "top", "author": "x", "score": 3, "created_utc": 1,
                                "parent_id": "t3_p1", "permalink": "/c1",
                                "replies": {"data": {"children": [
                                    {"kind": "t1", "data": {"id": "c2", "body": "[deleted]", "replies": {
                                        "data": {"children": [{"kind": "t1", "data": {"id": "c3", "body": "deep",
                                                                                      "replies": ""}}]}}}},
                                    {"kind": "more", "data": {"children": ["c9"]}},
                                ]}}}},
    ]
    out = list(flatten_comments(tree, "p1", "python"))
    assert [(c.id, c.depth) for c in out] == [("c1", 0), ("c3", 2)]


# -- HTTP edge cases (live Reddit is unreachable from CI, so these pin the behaviour against fakes) ----------

def test_oauth_expired_token_is_refreshed_once_on_401():
    c = client([FakeResponse(200, {"access_token": "old", "expires_in": 3600}),
                FakeResponse(401),
                FakeResponse(200, {"access_token": "new", "expires_in": 3600}),
                FakeResponse(200, listing([post(1)]))], client_id="id", client_secret="secret")
    assert len(c.fetch_posts("python", limit=1)) == 1
    assert [call[0] for call in c.session.calls].count("https://www.reddit.com/api/v1/access_token") == 2
    assert c._token == "new"


def test_oauth_bad_credentials_give_a_clear_error():
    c = client([FakeResponse(401)], client_id="id", client_secret="nope")
    with pytest.raises(RedditError, match="client id and secret"):
        c.fetch_posts("python")


def test_oauth_error_payload_without_token():
    c = client([FakeResponse(200, {"error": "invalid_grant"})], client_id="id", client_secret="s")
    with pytest.raises(RedditError, match="invalid_grant"):
        c.fetch_posts("python")


def test_oauth_sends_bearer_header_and_reuses_token():
    c = client([FakeResponse(200, {"access_token": "tok", "expires_in": 3600}),
                FakeResponse(200, listing([post(1)], after="t3_p1")),
                FakeResponse(200, listing([post(2)]))], client_id="id", client_secret="s")
    c.fetch_posts("python", limit=2)
    assert sum(u.endswith("access_token") for u, _ in c.session.calls) == 1


def test_anonymous_403_blames_access_oauth_403_blames_subreddit():
    with pytest.raises(RedditError, match="Settings"):
        client([FakeResponse(403)]).fetch_posts("python")
    c = client([FakeResponse(200, {"access_token": "t", "expires_in": 3600}), FakeResponse(403)],
               client_id="id", client_secret="s")
    with pytest.raises(RedditError, match="private or quarantined"):
        c.fetch_posts("python")


def test_404_names_the_subreddit_path():
    with pytest.raises(RedditError, match="Not found.*r/nope"):
        client([FakeResponse(404)]).fetch_posts("nope")


def test_5xx_is_retried_then_gives_up():
    c = client([FakeResponse(503), FakeResponse(502), FakeResponse(200, listing([post(1)]))])
    assert len(c.fetch_posts("python", limit=1)) == 1
    c = client([FakeResponse(500)] * 4)
    with pytest.raises(RedditError, match="Gave up"):
        c.fetch_posts("python")
    assert len(c.session.calls) == 4


def test_unexpected_status_raises():
    with pytest.raises(RedditError, match="HTTP 418"):
        client([FakeResponse(418)]).fetch_posts("python")


def test_429_without_a_usable_retry_after_still_backs_off():
    waits = []
    c = RedditClient(session=FakeSession([FakeResponse(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                                          FakeResponse(429),
                                          FakeResponse(200, listing([post(1)]))]),
                     sleep=waits.append, min_interval=0)
    assert len(c.fetch_posts("python", limit=1)) == 1
    assert all(0 < w <= 60 for w in waits) and len(waits) == 2


def test_429_retry_after_is_capped():
    waits = []
    c = RedditClient(session=FakeSession([FakeResponse(429, headers={"Retry-After": "900"}),
                                          FakeResponse(200, listing([post(1)]))]),
                     sleep=waits.append, min_interval=0)
    c.fetch_posts("python", limit=1)
    assert waits == [60.0]


def test_throttle_spaces_requests_by_min_interval():
    waits = []
    c = RedditClient(session=FakeSession([FakeResponse(200, listing([post(1)], after="t3_p1")),
                                          FakeResponse(200, listing([post(2)]))]),
                     sleep=waits.append, min_interval=6.0)
    c.fetch_posts("python", limit=2)
    assert len(waits) == 1 and 0 < waits[0] <= 6.0  # first request is free, the second waits


def test_default_intervals_match_the_documented_rates():
    assert RedditClient(session=FakeSession([])).min_interval == 6.0
    assert RedditClient(session=FakeSession([]), client_id="a", client_secret="b").min_interval == 0.7
    assert not RedditClient(session=FakeSession([]), client_id="a").authenticated  # secret missing -> anonymous


def test_should_stop_ends_pagination_early():
    stops = iter([False, True])
    c = client([FakeResponse(200, listing([post(i) for i in range(100)], after="t3_p99"))])
    posts = c.fetch_posts("python", limit=500, should_stop=lambda: next(stops))
    assert len(posts) == 100 and len(c.session.calls) == 1


def test_duplicate_posts_across_pages_and_non_post_children_are_skipped():
    more = {"kind": "t1", "data": {"id": "x"}}
    c = client([FakeResponse(200, listing([post(1), post(2), more], after="t3_p2")),
                FakeResponse(200, listing([post(2), post(3)]))])
    assert [p.id for p in c.fetch_posts("python", limit=10)] == ["p1", "p2", "p3"]


def test_empty_page_with_an_after_token_does_not_loop_forever():
    c = client([FakeResponse(200, listing([], after="t3_zzz"))])
    assert c.fetch_posts("python", limit=10) == []
    assert len(c.session.calls) == 1


@pytest.mark.parametrize("name", ["r/Python", "/r/Python", "/r/Python/", " Python ", "Python/", "R/Python",
                                  "https://www.reddit.com/r/Python/", "old.reddit.com/r/Python/top/?t=week"])
def test_subreddit_name_is_normalised(name):
    c = client([FakeResponse(200, listing([post(1)]))])
    c.fetch_posts(name, limit=1)
    assert c.session.calls[0][0] == "https://www.reddit.com/r/Python/hot.json"


@pytest.mark.parametrize("name", ["python dev", "a", "py-thon", "x" * 22])
def test_invalid_subreddit_names_are_rejected_before_any_request(name):
    c = client([])
    with pytest.raises(RedditError, match="isn't a valid subreddit name"):
        c.fetch_posts(name)
    assert c.session.calls == []


def test_blank_subreddit_is_rejected_before_any_request():
    c = client([])
    with pytest.raises(RedditError, match="Enter a subreddit"):
        c.fetch_posts("  r/ ")
    assert c.session.calls == []


def test_time_filter_only_sent_for_top_and_controversial():
    for sort, sent in [("top", True), ("controversial", True), ("hot", False), ("new", False), ("rising", False)]:
        c = client([FakeResponse(200, listing([post(1)]))])
        c.fetch_posts("python", sort=sort, time_filter="year", limit=1)
        assert ("t" in c.session.calls[0][1]) is sent, sort


def test_search_keeps_sorts_the_search_endpoint_supports():
    c = client([FakeResponse(200, listing([post(1)]))])
    c.fetch_posts("python", sort="top", query="x", limit=1)
    assert c.session.calls[0][1]["sort"] == "top"


def test_posts_tolerate_missing_and_null_fields():
    bare = {"kind": "t3", "data": {"id": "p9", "title": "t", "selftext": None, "upvote_ratio": None}}
    p = client([FakeResponse(200, listing([bare]))]).fetch_posts("python", limit=1)[0]
    assert (p.selftext, p.upvote_ratio, p.author, p.score) == ("", 0.0, "[deleted]", 0)
    assert p.permalink == "https://www.reddit.com"
    assert p.text == "t"


def test_posts_tolerate_null_score_and_author():
    """Reddit sends null for a deleted author and can hide scores; the fetch must not die on them."""
    odd = {"kind": "t3", "data": {"id": "p8", "subreddit": "Python", "title": "t", "author": None, "score": None,
                                  "num_comments": None, "created_utc": None}}
    p = client([FakeResponse(200, listing([odd]))]).fetch_posts("python", limit=1)[0]
    assert (p.author, p.score, p.num_comments, p.created_utc) == ("[deleted]", 0, 0, 0.0)


def test_fetch_comments_flattens_second_listing_and_honours_limit():
    def c_(i):
        return {"kind": "t1", "data": {"id": f"c{i}", "body": f"b{i}", "replies": ""}}
    tree = [listing([post(1)]), listing([c_(1), c_(2), c_(3)])]
    c = client([FakeResponse(200, tree)])
    p = _post_obj()
    out = c.fetch_comments(p, limit=2)
    assert [x.id for x in out] == ["c1", "c2"] and out[0].post_id == "p1"
    assert c.session.calls[0][0] == "https://www.reddit.com/r/Python/comments/p1.json"


def test_fetch_comments_on_malformed_payload_is_empty():
    assert client([FakeResponse(200, {"error": 1})]).fetch_comments(_post_obj()) == []
    assert client([FakeResponse(200, [listing([])])]).fetch_comments(_post_obj()) == []


def test_comments_tolerate_null_author_and_score():
    tree = [{"kind": "t1", "data": {"id": "c1", "body": "hi", "author": None, "score": None,
                                    "created_utc": None, "replies": ""}}]
    (c,) = flatten_comments(tree, "p1", "python")
    assert (c.author, c.score, c.created_utc) == ("[deleted]", 0, 0.0)


def _post_obj():
    from subsentiment.reddit import _post_from
    return _post_from(post(1)["data"])
