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

    def _make(posts, **kw):
        a = FakeArchive(posts, **kw)
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
    # The search's `after` is exclusive (checked live): asking it for after=<midnight> leaves out a post
    # made at 00:00:00, so top mode never saw it. The day counts must still ask from midnight itself, or
    # the archive's buckets come back empty.
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
    pressed = []

    def press_once_a_search_is_out():
        # Pressed while a search is in flight, timed from the press: a fixed 1 s timer timed from before the
        # store was made counted a busy Windows runner's setup as waiting.
        deadline = time.monotonic() + 20
        while "/api/posts/search" not in a.paths() and time.monotonic() < deadline:
            time.sleep(0.01)
        pressed.append(time.monotonic())
        stop.set()
    threading.Thread(target=press_once_a_search_is_out, daemon=True).start()
    result = collect.run(arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0.05)),
                         Store(tmp_path / "s.db"),
                         collect.FetchRequest("python", after=time.time() - 7 * DAY, limit=100,
                                              include_comments=False), should_stop=stop.is_set)
    assert result.stopped and "/api/posts/search" in a.paths()
    assert time.monotonic() - pressed[0] < 3  # 8 s before: it waited for every search in flight


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


def quick_client():
    return arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0), workers=1)


def test_a_day_the_range_cuts_short_is_sampled_from_the_whole_day(archive, tmp_path):
    # Sampling the afternoon of a day sized its sample from the afternoon's thread alone (278 of its 1,000) and held
    # it; a fetch of the whole day kept those 278 beside the morning's 165, where the whole day gives it 158. Holding
    # such a day back instead gave it nothing, every time: a range from the Fetch form starts at a local midnight (here
    # 05:00 UTC, as at UTC-5) or N days ago, so its first and last UTC days are cut short.
    archive([post("early", DAY0 + 3600, num_comments=1000), post("late", DAY0 + 20 * 3600, num_comments=1000),
             post("next", DAY0 + DAY + 3600, num_comments=1000), post("next2", DAY0 + DAY + 20 * 3600,
                                                                     num_comments=1000)], rate=(10000, 1.0))
    s, whole = Store(tmp_path / "s.db"), Store(tmp_path / "whole.db")
    ids = ("early", "late", "next", "next2")
    collect.run(quick_client(), s, collect.FetchRequest("Sub", after=DAY0 + 5 * 3600, before=DAY0 + DAY + 5 * 3600,
                                                        limit=None, comment_pick=collect.SAMPLE))
    req = collect.FetchRequest("Sub", after=DAY0, before=DAY0 + 2 * DAY, limit=None, comment_pick=collect.SAMPLE)
    collect.run(quick_client(), whole, req)
    want = {p: collect.Sample(whole.comment_totals("Sub", d, d + DAY)).quota(p)
            for d in (DAY0, DAY0 + DAY) for p in ids if p in whole.comment_totals("Sub", d, d + DAY)}
    assert all(want.values()) and {p: len(s.comment_ids(p)) for p in ids} == want
    assert s.complete_keys("sampled", "sub") == set(ids)
    collect.run(quick_client(), s, req)  # the whole days again: nothing more to add
    assert {p: len(s.comment_ids(p)) for p in ids} == {p: len(whole.comment_ids(p)) for p in ids} == want


def test_a_day_that_just_ended_isnt_complete_while_the_archive_can_still_take_posts_in(archive, tmp_path,
                                                                                         monkeypatch):
    # At 00:10 UTC the day before was marked downloaded in full, so a post the archive took in a few minutes
    # later was never fetched: complete days aren't read again.
    day = DAY0 + 2 * DAY
    monkeypatch.setattr(time, "time", lambda: float(day + DAY + 600))
    a = archive([post("p1", day + 3600)])
    s = Store(tmp_path / "s.db")
    req = collect.FetchRequest("Sub", after=day - 5 * DAY, limit=None, include_comments=False)
    collect.run(quick_client(), s, req)
    assert str(day) not in s.complete_keys("posts", "Sub") and str(day - DAY) in s.complete_keys("posts", "Sub")
    a.posts.append(post("late", day + DAY - 60))  # taken in late
    assert collect.run(quick_client(), s, req).posts == 1
    assert {r["id"] for r in s.posts_between("Sub", day, day + DAY)} == {"p1", "late"}


@pytest.mark.parametrize("cap", [200, 500])
def test_a_cap_bigger_than_half_a_tree_request_is_met_on_a_big_thread(archive, tmp_path, cap):
    # The tree is asked for TREE_LIMIT (300) comments at most, and a bigger thread comes back about half expanded:
    # 150 were saved whatever the cap above that, and the post was never asked about again.
    a = archive([post("big", DAY0 + 3600, num_comments=1000)], rate=(10000, 1.0))
    s = Store(tmp_path / "s.db")
    req = collect.FetchRequest("Sub", after=DAY0, before=DAY0 + DAY, limit=None, comments_per_post=cap)
    assert collect.run(quick_client(), s, req).comments == cap
    a.clear()
    collect.run(quick_client(), s, req)
    assert len(s.comment_ids("big")) == cap and "/api/comments/tree" not in a.paths()  # done with


def test_refreshing_scores_keeps_a_capped_thread_to_its_cap(archive, tmp_path):
    # The post's scores settled, so its thread was picked again: the new top 50 were saved beside the old ones.
    young = post("young", DAY0 + 3600, score=1, num_comments=0, retrieved_on=DAY0 + 3660, tree=200)
    archive([young], rate=(10000, 1.0))
    s = Store(tmp_path / "s.db")
    req = collect.FetchRequest("Sub", after=DAY0, before=DAY0 + DAY, limit=None, comments_per_post=50)
    collect.run(quick_client(), s, req)
    young.update(score=50, num_comments=200, retrieved_on=DAY0 + 40 * 3600)
    assert collect.run(quick_client(), s, dataclasses.replace(req, refresh_scores=True)).updated == 1
    assert len(s.comment_ids("young")) == 50


def test_stop_keeps_the_posts_a_slow_slice_already_downloaded(archive, tmp_path):
    # A busy day downloads slowly, a page at a time. Stop stopped waiting for the slices still running and
    # dropped everything they had, so a Download all stopped after minutes saved nothing.
    # 8 slices of 300 posts, 3 pages each; Stop comes once the first four have asked for their second page
    a = archive([post(f"p{n}", DAY0 + n * 72) for n in range(2400)])
    a.delays["/api/posts/search"] = 1.0
    stop = threading.Event()

    def press_once_the_second_pages_are_asked_for():
        # The first search (the whole range), each slice's first page, then their second: 9 requests. A fixed
        # 2.5 s timer pressed Stop before any first page was back on a loaded Windows runner.
        deadline = time.monotonic() + 30
        while a.paths().count("/api/posts/search") < 9 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
    threading.Thread(target=press_once_the_second_pages_are_asked_for, daemon=True).start()
    store = Store(tmp_path / "s.db")
    result = collect.run(arctic_shift.ArcticShiftClient(limiter=arctic_shift.RateLimiter(0)), store,
                         collect.FetchRequest("Sub", after=DAY0, before=DAY0 + 2 * DAY, limit=2400,
                                              include_comments=False), should_stop=stop.is_set)
    assert result.stopped
    assert result.posts >= 400  # the slices running had at least their first page each
    assert len(store.posts("Sub")) == result.posts
