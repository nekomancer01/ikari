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
