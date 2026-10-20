"""Fetching against a fake Arctic Shift on localhost, at the edges the real archive has: posts sharing a
second, posts right on a day's start, odd records, slow answers while Stop is pressed, and the next job
after a stopped one. Each was found failing on main in the 20 Oct 2026 bug hunt."""
import dataclasses
import threading
import time

import pytest

from subsentiment import arctic_shift, collect, sentiment
from subsentiment.store import Store

from fake_archive import DAY, FakeArchive, make_posts

DAY0 = int(time.time()) // DAY * DAY - 5 * DAY  # a settled day: five days ago


def post(pid, created, sub="Sub", removed=False, **extra):
    d = {"id": pid, "subreddit": sub, "title": f"title {pid}", "selftext": "", "author": "x", "score": 5,
         "num_comments": 0, "upvote_ratio": 0.9, "created_utc": created, "retrieved_on": created + 200000,
         "permalink": f"/r/{sub}/comments/{pid}/", "url": ""}
    if removed:
        d["removed_by_category"] = "automod"
    d.update(extra)
    return d


@pytest.fixture
def archive(monkeypatch):
    made = []

    def _make(posts):
        a = FakeArchive(posts)
        monkeypatch.setattr(arctic_shift, "BASE_URL", a.url)
        monkeypatch.setattr(arctic_shift, "MAX_WAIT", 1.2)
        made.append(a)
        return a
    yield _make
    for a in made:
        a.close()


def test_a_burst_of_posts_in_one_second_doesnt_end_the_day_early(archive):
    # 12 posts in one second, the first 8 removed (a spam burst). The small first page held only posts
    # from that second, the next page from a second earlier was the same page, and the slice was taken
    # as read to its end: the 4 usable posts were lost and the day could be marked complete.
    t = DAY0 + 5000
    archive([post(f"p{k:02d}", t, removed=k < 8) for k in range(12)] + [post("later", t + 60)])
    c = arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0), workers=1)
    got = c.fetch_posts("Sub", DAY0, DAY0 + DAY, limit=5, exhaust=True)
    assert sorted(p.id for p in got) == ["later", "p08", "p09", "p10", "p11"]


def test_a_post_made_at_midnight_is_counted_and_can_be_a_top_post(archive):
    # The archive's `after` is exclusive (checked live): asking for after=<midnight> leaves out a post
    # made at 00:00:00, so the day's count was one short and top mode never saw it.
    archive([post("midnight", DAY0, score=999), post("noon", DAY0 + 43200, score=3),
             post("evening", DAY0 + 70000, score=2)])
    c = arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0), workers=1)
    assert c.day_counts("Sub", DAY0, DAY0 + DAY) == {DAY0: 3}
    top = c.fetch_posts("Sub", DAY0, DAY0 + DAY, limit=1, top=True)
    assert [p.id for p in top] == ["midnight"]
    every = c.fetch_posts("Sub", DAY0, DAY0 + DAY, limit=10)
    assert sorted(p.id for p in every) == ["evening", "midnight", "noon"]  # each once


def test_odd_records_are_skipped_or_read_rather_than_failing_the_fetch(archive):
    archive([post("p1", DAY0 + 100, score="1.5", num_comments="abc", _meta="?", permalink=None),
             post("p2", DAY0 + 200, author=7, url=None)])
    c = arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0), workers=1)
    got = {p.id: p for p in c.fetch_posts("Sub", DAY0, DAY0 + DAY, limit=10)}
    assert got["p1"].score == 1 and got["p1"].num_comments == 0
    assert got["p1"].permalink == "https://www.reddit.com/r/Sub/comments/p1/"
    assert got["p2"].author == "[deleted]" and got["p2"].url == ""
    tree = [{"kind": "t1", "data": {"id": "c1", "body": "hi", "depth": None, "created_utc": DAY0 + 300,
                                    "replies": {"data": {"children": [None, {"kind": "t1", "data": None}]}}}}]
    assert [d["depth"] for d in arctic_shift._walk(tree)] == [0, 1]


def test_stop_doesnt_wait_for_a_slow_search(archive, tmp_path):
    a = archive(make_posts("python", time.time(), days=10))
    a.delays["/api/posts/search"] = 8.0
    stop = threading.Event()
    threading.Timer(1.0, stop.set).start()
    start = time.monotonic()
    result = collect.run(arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0.05)),
                         Store(tmp_path / "s.db"),
                         collect.FetchRequest("python", after=time.time() - 7 * DAY, limit=100,
                                              include_comments=False), should_stop=stop.is_set)
    assert result.stopped
    assert time.monotonic() - start < 4  # 8 s before: it waited for every search in flight


def test_requests_left_out_by_stop_dont_hold_up_the_next_job(archive, tmp_path):
    # Stop leaves comment-tree requests running. They still counted as in flight, so once the server's
    # request window reset (the budget unknown again), the next fetch or count sent nothing until they
    # came back: up to a minute.
    now = time.time()
    a = archive(make_posts("python", now, days=6))
    a.rate = (60, 1.0)
    a.delays["/api/comments/tree"] = 12.0
    limiter = arctic_shift.RateLimiter(0.05)
    stop = threading.Event()

    def stop_once_trees_start():
        while "/api/comments/tree" not in a.paths():
            time.sleep(0.05)
        time.sleep(0.3)
        stop.set()
    threading.Thread(target=stop_once_trees_start, daemon=True).start()
    collect.run(arctic_shift.ArcticShiftClient(limiter=limiter), Store(tmp_path / "s.db"),
                collect.FetchRequest("python", after=now - 5 * DAY, before=now, limit=20), should_stop=stop.is_set)
    time.sleep(1.5)  # the server's request window resets
    start = time.monotonic()
    arctic_shift.ArcticShiftClient(limiter=limiter).day_counts("python", now - 3 * DAY, now)
    assert time.monotonic() - start < 3  # 10 s and more before


def test_top_mode_keeps_a_settled_score_it_downloaded_for_a_saved_post(archive, tmp_path):
    p = post("abc", DAY0 + 100, score=480)
    archive([p])
    s = Store(tmp_path / "s.db")
    early = dataclasses.replace(arctic_shift._post(p), score=None, upvote_ratio=None, num_comments=None)
    s.save_post(early, *sentiment.score(early.text))  # saved before the archive re-checked it
    s.commit()
    result = collect.run(arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0), workers=1), s,
                         collect.FetchRequest("Sub", after=DAY0, before=DAY0 + DAY, limit=5, top=True,
                                              include_comments=False))
    assert s.posts("Sub")[0]["score"] == 480  # None before: the settled copy was dropped
    assert result.updated == 1


def test_more_posts_in_one_second_than_a_page_holds_never_marks_the_range_read_to_its_end(archive):
    t = DAY0 + 5000
    archive([post(f"p{k:03d}", t) for k in range(arctic_shift.PAGE + 20)] + [post("later", t + 60)])
    c = arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0), workers=1)
    got = c.fetch_posts("Sub", DAY0, DAY0 + DAY, limit=1000, exhaust=True)
    assert "later" in {p.id for p in got}  # it moves on rather than asking for that second forever
    assert not c.exhausted  # some of that second's posts were never seen, so the day isn't complete


def test_numeric_ids_and_infinite_numbers_are_read(archive):
    archive([post(12345, DAY0 + 100, score=float("inf")), post("p2", DAY0 + 200, upvote_ratio=float("nan"))])
    c = arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0), workers=1)
    got = {p.id: p for p in c.fetch_posts("Sub", DAY0, DAY0 + DAY, limit=10, skip={"12345"})}
    assert set(got) == {"p2"} and got["p2"].upvote_ratio == 0.0  # the saved one is recognised and skipped
