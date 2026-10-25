"""The fetch job: what gets refreshed, what gets skipped, and how comment failures are handled."""
import threading
import time

from subsentiment import collect
from subsentiment.arctic_shift import RateLimiter
from subsentiment.reddit import Comment, FetchError, Post
from subsentiment.store import Store

T = 1_790_000_000.0


def mk(i, score=10, num_comments=2):
    return Post(f"p{i}", "Python", f"title {i}", "", "a", score, None if score is None else 0.9, num_comments,
                T + i, f"https://r/{i}", "")


class FakeClient:
    def __init__(self, posts=(), by_id=(), failing=()):
        self.posts = list(posts)
        self.by_id = {p.id: p for p in by_id}
        self.failing = set(failing)
        self.calls = []

    def fetch_posts_by_ids(self, ids, should_stop=None):
        self.calls.append(("ids", list(ids)))
        return [self.by_id[i] for i in ids if i in self.by_id]

    def fetch_posts(self, subreddit, after, before, limit, query, top=False, skip=(), progress=None,
                    should_stop=None, on_posts=None, exhaust=False, read=()):
        self.calls.append(("posts", top, set(skip)))
        left = [p for p in self.posts if top or p.id not in skip]
        picked = left[:limit]
        self.exhausted = not top and len(picked) == len(left)
        for p in picked:  # the real client hands over each page as it is downloaded
            if on_posts:
                on_posts([p])
        return picked

    def fetch_comments(self, post, limit, pick="top"):
        self.calls.append(("comments", post.id))
        if post.id in self.failing:
            raise FetchError("Arctic Shift: Query timed out")
        return [Comment(f"c{post.id}", post.id, "Python", "t3_" + post.id, "b", "lovely", 1, T, 0, "u")]


def store_with(tmp_path, posts, comments_for=()):
    s = Store(tmp_path / "x.db")
    for p in posts:
        s.save_post(p, 0.0, "neutral")
    for pid in comments_for:
        s.save_comment(Comment(f"old{pid}", pid, "Python", "t3_" + pid, "b", "hi", 1, T, 0, "u"), 0.0, "neutral")
    s.commit()
    return s


def test_unsettled_saved_posts_are_refreshed_in_one_batch_and_get_comments(tmp_path):
    s = store_with(tmp_path, [mk(1, score=None, num_comments=None), mk(2)], comments_for=["p1", "p2"])
    c = FakeClient(by_id=[mk(1, score=77, num_comments=5)])
    result = collect.run(c, s, collect.FetchRequest("python", refresh_scores=True))
    assert c.calls[0] == ("ids", ["p1"])
    assert {r["id"]: r["score"] for r in s.posts("python")} == {"p1": 77, "p2": 10}
    assert result.updated == 1 and result.posts == 0
    assert ("comments", "p1") in c.calls and ("comments", "p2") not in c.calls


def test_saved_scores_are_only_rechecked_when_asked(tmp_path):
    s = store_with(tmp_path, [mk(1, score=None, num_comments=None)], comments_for=["p1"])
    c = FakeClient(by_id=[mk(1, score=77)])
    result = collect.run(c, s, collect.FetchRequest("python"))
    assert not any(call[0] == "ids" for call in c.calls) and result.updated == 0


def test_an_even_sample_skips_saved_posts(tmp_path):
    s = store_with(tmp_path, [mk(1)], comments_for=["p1"])
    c = FakeClient(posts=[mk(1), mk(2), mk(3)])
    result = collect.run(c, s, collect.FetchRequest("python", limit=2))
    assert ("posts", False, {"p1"}) in c.calls
    assert (result.posts, result.already) == (2, 0)
    assert len(s.posts("python")) == 3


def test_top_mode_counts_saved_posts_without_saving_them_again(tmp_path):
    s = store_with(tmp_path, [mk(1)], comments_for=["p1"])
    c = FakeClient(posts=[mk(1), mk(2)])
    result = collect.run(c, s, collect.FetchRequest("python", top=True))
    assert ("posts", True, set()) in c.calls
    assert (result.posts, result.already, result.comments) == (1, 1, 1)


def test_one_post_failing_doesnt_stop_the_others_comments(tmp_path):
    s = store_with(tmp_path, [])
    c = FakeClient(posts=[mk(1), mk(2), mk(3)], failing={"p2"})
    result = collect.run(c, s, collect.FetchRequest("python"))
    assert (result.posts, result.comments, result.comment_errors) == (3, 2, 1)


def test_comments_stop_after_repeated_failures_and_are_retried_next_time(tmp_path):
    s = store_with(tmp_path, [])
    posts = [mk(i) for i in range(8)]
    c = FakeClient(posts=posts, failing={p.id for p in posts})
    result = collect.run(c, s, collect.FetchRequest("python"))
    assert sum(1 for call in c.calls if call[0] == "comments") == collect.GIVE_UP_AFTER
    assert (result.posts, result.comments, result.comment_errors) == (8, 0, 8)

    c = FakeClient(posts=posts)  # the archive has recovered; nothing new to fetch, but comments are missing
    result = collect.run(c, s, collect.FetchRequest("python"))
    assert (result.posts, result.comments, result.comment_errors) == (0, 8, 0)


def test_a_thread_stopped_while_its_comments_were_scored_is_left_unasked_like_one_stopped_before(tmp_path):
    s = store_with(tmp_path, [])
    collect.run(FakeClient(posts=[mk(1)]), s, collect.FetchRequest("python"), score_all=lambda lexicon, texts: None)
    assert s.comments("python") == [] and s.posts_between("python")[0]["comments_ok"] is None
    c = FakeClient(posts=[mk(1)])
    result = collect.run(c, s, collect.FetchRequest("python", top=True))  # picked again, its comments are asked for
    assert ("comments", "p1") in c.calls and result.comments == 1

def test_posts_known_to_have_no_comments_are_not_looked_up(tmp_path):
    s = store_with(tmp_path, [])
    c = FakeClient(posts=[mk(1, num_comments=0), mk(2, score=None, num_comments=None)])
    collect.run(c, s, collect.FetchRequest("python"))
    assert [call[1] for call in c.calls if call[0] == "comments"] == ["p2"]


def test_a_post_whose_comments_came_back_empty_isnt_asked_again(tmp_path):
    s = store_with(tmp_path, [])

    class Quiet(FakeClient):
        def fetch_comments(self, post, limit, pick="top"):
            self.calls.append(("comments", post.id))
            return []
    c = Quiet(posts=[mk(1, score=None, num_comments=None)])
    collect.run(c, s, collect.FetchRequest("python"))
    c2 = Quiet(posts=[mk(1, score=None, num_comments=None)])
    collect.run(c2, s, collect.FetchRequest("python"))
    assert [call for call in c2.calls if call[0] == "comments"] == []


def test_saved_is_reported_after_each_batch(tmp_path):
    s = store_with(tmp_path, [])
    seen = []
    collect.run(FakeClient(posts=[mk(1), mk(2)]), s, collect.FetchRequest("python"), saved=lambda: seen.append(1))
    assert len(seen) == 5  # each post as it arrives, the batch commit, then each comment


class FullThreadClient(FakeClient):
    def __init__(self, whole=True, **kw):
        super().__init__(**kw)
        self.whole = whole

    def fetch_all_comments(self, post, cap=0, pick="top"):
        self.calls.append(("all", post.id, post.num_comments))
        if post.id in self.failing:
            raise FetchError("Arctic Shift: Query timed out")
        return [Comment(f"c{post.id}{j}", post.id, "Python", "t3_" + post.id, "b", "ok", 1, T, 0, "u")
                for j in range(3)], self.whole


def test_all_comments_rechecks_unsettled_posts_then_marks_settled_threads_complete(tmp_path):
    s = store_with(tmp_path, [mk(1, score=None, num_comments=None), mk(2), mk(3)])
    c = FullThreadClient(by_id=[mk(1, score=5, num_comments=3)])
    result = collect.run(c, s, collect.FetchRequest("python", comment_posts=("p1", "p2")))
    assert c.calls[0] == ("ids", ["p1"])
    assert ("all", "p1", 3) in c.calls and ("all", "p2", 2) in c.calls  # p1 with its fresh count
    assert not any(call[0] == "posts" for call in c.calls)  # no post search
    assert result.comments == 6 and result.updated == 1
    assert s.complete_keys("comments", "Python") == {"p1", "p2"}
    assert {r["id"]: r["saved_comments"] for r in s.comment_coverage("python", 0, 2e9)} == {"p1": 3, "p2": 3, "p3": 0}


def test_a_cut_short_or_unsettled_thread_is_not_marked_complete(tmp_path):
    s = store_with(tmp_path, [mk(1, score=None, num_comments=None), mk(2)])
    collect.run(FullThreadClient(), s, collect.FetchRequest("python", comment_posts=("p1",)))
    collect.run(FullThreadClient(whole=False), s, collect.FetchRequest("python", comment_posts=("p2",)))
    assert s.complete_keys("comments", "python") == set()
    assert s.complete_keys("comments_so_far", "python") == {"p1"}  # whole for now, so not offered again yet


def test_downloading_a_whole_day_marks_it_complete_only_when_the_archive_ran_out(tmp_path):
    day = T // 86400 * 86400  # the UTC day the posts are on
    s = store_with(tmp_path, [mk(1)])
    c = FakeClient(posts=[mk(1), mk(2), mk(3)])
    collect.run(c, s, collect.FetchRequest("python", after=day, before=day + 86400, limit=5, everything=True,
                                           include_comments=False))
    assert s.complete_keys("posts", "python") == {str(int(day))}

    s2 = store_with(tmp_path / "b", [])
    collect.run(FakeClient(posts=[mk(1), mk(2), mk(3)]), s2,
                collect.FetchRequest("python", after=day, before=day + 86400, limit=2, everything=True,
                                     include_comments=False))
    assert s2.complete_keys("posts", "python") == set()  # it may have had more than the 2 asked for


def test_a_whole_day_that_stopped_short_of_the_limit_is_not_marked_complete(tmp_path):
    # Sat 17 Oct: Download all saved 1,859 posts, well under its limit, but the archive had more it never
    # read. Getting fewer than asked for doesn't mean the archive ran out; only the client can say that.
    class ShortClient(FakeClient):
        def fetch_posts(self, *a, **kw):
            picked = super().fetch_posts(*a, **kw)[:2]
            self.exhausted = False
            return picked

    s = store_with(tmp_path, [])
    collect.run(ShortClient(posts=[mk(1), mk(2), mk(3)]), s,
                collect.FetchRequest("python", after=T, before=T + 86400, limit=10000, everything=True,
                                     include_comments=False))
    assert s.complete_keys("posts", "python") == set()


def test_each_post_and_comment_is_saved_and_announced_as_it_arrives(tmp_path):
    s = store_with(tmp_path, [])
    counts = []
    collect.run(FakeClient(posts=[mk(1), mk(2), mk(3)]), s, collect.FetchRequest("python"),
                saved=lambda: counts.append(len(s.posts("python"))))
    assert counts[:3] == [1, 2, 3]  # the table grows one post at a time


def test_a_batch_of_posts_is_saved_in_one_commit(tmp_path):
    # After Stop, the posts slices still running had come as one batch of hundreds, each saved in a commit of its
    # own: on a Windows runner that took over 30 s, so Stop seemed not to work.
    class Batch(FakeClient):
        def fetch_posts(self, *a, on_posts=None, **kw):
            on_posts(self.posts)
            self.exhausted = True
            return self.posts
    s = store_with(tmp_path, [])
    commits, real = [], s.commit
    s.commit = lambda: (commits.append(len(s.posts("python"))), real())[1]
    announced = []
    collect.run(Batch(posts=[mk(i) for i in range(300)]), s, collect.FetchRequest("python", include_comments=False),
                saved=lambda: announced.append(len(s.posts("python"))))
    assert announced[0] == 300 and commits.count(300) <= 3


def test_a_fetch_does_not_download_comments_for_saved_posts_nobody_asked_about(tmp_path):
    s = store_with(tmp_path, [mk(1), mk(2)])  # saved earlier, comments never requested
    c = FakeClient(posts=[mk(3)])
    collect.run(c, s, collect.FetchRequest("python"))
    assert [call[1] for call in c.calls if call[0] == "comments"] == ["p3"]


def test_top_mode_gets_comments_for_a_picked_saved_post_that_never_had_them(tmp_path):
    s = store_with(tmp_path, [mk(1), mk(2)])  # saved earlier, comments never requested
    c = FakeClient(posts=[mk(1), mk(3)])
    collect.run(c, s, collect.FetchRequest("python", top=True))  # top mode picks p1 again, plus p3
    assert sorted(call[1] for call in c.calls if call[0] == "comments") == ["p1", "p3"]  # not p2


def test_today_is_never_marked_complete(tmp_path):
    import time
    s = store_with(tmp_path, [])
    start = time.time() // 86400 * 86400
    collect.run(FakeClient(posts=[mk(1)]), s, collect.FetchRequest("python", after=start, before=start + 86400,
                                                                  limit=50, everything=True, include_comments=False))
    assert s.complete_keys("posts", "python") == set()


class SlowClient(FakeClient):
    """Fetches comments for up to ``workers`` posts at once, each taking a moment, like the real client.

    The first calls wait (up to 5 s) for ``workers`` of them to be running together. A fixed 30 ms nap let a
    busy Windows runner start the later threads after the first had finished, so only 2 ever overlapped."""
    workers = 4

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.busy = self.most = 0
        self.waited = False  # the 5 s ran out once: the rest don't wait
        self.lock = threading.Condition()

    def fetch_comments(self, post, limit, pick="top"):
        with self.lock:
            self.busy += 1
            self.most = max(self.most, self.busy)
            self.lock.notify_all()
            if not self.lock.wait_for(lambda: self.most >= self.workers or self.waited, timeout=5):
                self.waited = True
        time.sleep(0.03)
        try:
            return super().fetch_comments(post, limit)
        finally:
            with self.lock:
                self.busy -= 1


def test_comments_for_several_posts_are_fetched_at_once_and_all_saved(tmp_path):
    s = store_with(tmp_path, [])  # a SQLite connection only works on its own thread: saving must stay here
    c = SlowClient(posts=[mk(i) for i in range(12)])
    result = collect.run(c, s, collect.FetchRequest("python"))
    assert c.most == 4
    assert (result.posts, result.comments, result.comment_errors) == (12, 12, 0)
    assert len(s.comments("python")) == 12


def test_comments_fetched_at_once_still_give_up_after_repeated_failures(tmp_path):
    s = store_with(tmp_path, [])
    posts = [mk(i) for i in range(20)]
    c = SlowClient(posts=posts, failing={p.id for p in posts})
    result = collect.run(c, s, collect.FetchRequest("python"))
    tried = sum(1 for call in c.calls if call[0] == "comments")
    assert collect.GIVE_UP_AFTER <= tried < collect.GIVE_UP_AFTER + c.workers  # only those already under way
    assert result.comment_errors == 20
    assert all(r["comments_ok"] == 0 for r in s.posts_between("python", None, None))


def test_stop_doesnt_wait_for_a_comment_tree_still_downloading(tmp_path):
    # One huge thread takes the archive ages to send; Stop shouldn't sit it out.
    huge = threading.Event()

    class HugeThread(SlowClient):
        def fetch_comments(self, post, limit, pick="top"):
            if post.id == "p0":
                huge.set()
                time.sleep(3)
            return super().fetch_comments(post, limit)

    # Stop is pressed once the huge thread is downloading and timed from there. A timer started with the fetch
    # also counted saving the posts first, which took Windows runners 2-3 s (each commit waits on the disk).
    s = store_with(tmp_path, [])
    stop, pressed = threading.Event(), []

    def press_once_the_huge_thread_is_out():
        huge.wait(10)
        pressed.append(time.perf_counter())
        stop.set()
    threading.Thread(target=press_once_the_huge_thread_is_out, daemon=True).start()
    result = collect.run(HugeThread(posts=[mk(i) for i in range(12)]), s, collect.FetchRequest("python"),
                         should_stop=stop.is_set)
    assert pressed and time.perf_counter() - pressed[0] < 1.5 and result.stopped
    assert s.posts("python") and result.comment_errors == 0  # what finished is kept, nothing marked failed


D0 = int(T // 86400 * 86400)  # a UTC midnight, long past


class DayClient(FakeClient):
    """Posts spread over several UTC days; each fetch_posts call returns the ones in its window."""

    def __init__(self, per_day=3, days=4, stop_after=None, num_comments=0):
        super().__init__([Post(f"d{d}p{k}", "Python", "t", "", "a", 5, 0.9, num_comments,
                               D0 + d * 86400 + 3600 * (k + 1), "x", "") for d in range(days) for k in range(per_day)])
        self.windows = []
        self.stop_after = stop_after

    def fetch_posts(self, subreddit, after, before, limit, query, top=False, skip=(), progress=None,
                    should_stop=None, on_posts=None, exhaust=False, read=()):
        self.windows.append((after, before, limit, exhaust))
        self.calls.append(("posts", after))
        if progress:
            progress("Collecting posts")
        got = [p for p in self.posts if after <= p.created_utc < before and p.id not in skip][:limit]
        self.exhausted = True
        for p in got:
            on_posts([p])
        return got


def test_a_whole_range_is_downloaded_a_day_at_a_time_like_download_all(tmp_path):
    s = store_with(tmp_path, [])
    c = DayClient()
    shown = []
    result = collect.run(c, s, collect.FetchRequest("python", after=D0 + 7200, before=D0 + 4 * 86400, limit=None,
                                                    include_comments=False),
                         progress=lambda done, total, msg: shown.append((done, total, msg)))
    # Newest day first, every post of each (to Download all's cap), the first day only from 02:00. Three posts a day
    # is quiet, so once the newest day says so, the next ones are read together, two, then up to four.
    assert c.windows == [(D0 + 3 * 86400, D0 + 4 * 86400, collect.MAX_DAY_POSTS, True),
                         (D0 + 86400, D0 + 3 * 86400, 2 * collect.MAX_DAY_POSTS, True),
                         (D0 + 7200, D0 + 86400, collect.MAX_DAY_POSTS, True)]
    assert result.posts == 11 and len(s.posts("python")) == 11  # all but d0p0, posted at 01:00
    # The three whole days read to their end show as complete in Download coverage; the cut one doesn't.
    assert s.complete_keys("posts", "python") == {str(D0 + d * 86400) for d in (1, 2, 3)}
    days = [(done, total) for done, total, msg in shown if total]
    assert days[0] == (0, 4) and days[-1] == (4, 4) and shown[0][2].startswith("Day 1 of 4, ")


def test_days_already_downloaded_in_full_are_not_asked_for_again(tmp_path):
    s = store_with(tmp_path, [])
    s.mark_complete("posts", "python", str(D0 + 86400))
    s.commit()
    c = DayClient()
    collect.run(c, s, collect.FetchRequest("python", after=D0, before=D0 + 3 * 86400, limit=None,
                                           include_comments=False))
    assert [w[0] for w in c.windows] == [D0 + 2 * 86400, D0]


def test_a_keyword_search_of_a_whole_range_marks_no_day_complete(tmp_path):
    s = store_with(tmp_path, [])
    collect.run(DayClient(), s, collect.FetchRequest("python", after=D0, before=D0 + 2 * 86400, limit=None,
                                                     query="title", include_comments=False))
    assert s.complete_keys("posts", "python") == set()


def test_stopping_a_whole_range_keeps_what_was_saved_and_asks_for_no_more_days(tmp_path):
    s = store_with(tmp_path, [])
    c = DayClient()
    stop = threading.Event()
    on_posts_saved = []

    def saved():
        on_posts_saved.append(1)
        if len(on_posts_saved) == 2:  # Stop pressed partway through the newest day
            stop.set()
    result = collect.run(c, s, collect.FetchRequest("python", after=D0, before=D0 + 4 * 86400, limit=None),
                         should_stop=stop.is_set, saved=saved)
    assert len(c.windows) == 1 and result.stopped
    assert len(s.posts("python")) == 3  # the newest day's posts, all saved before the fetch wound down
    assert s.complete_keys("posts", "python") == set()  # stopped, so not known to be complete
    assert not any(call[0] == "comments" for call in c.calls)


def test_all_comments_on_a_fetch_get_every_thread_in_the_range_until_it_is_whole(tmp_path):
    # p1 was saved with its top comments only, p2's whole thread is already saved, p3 is new.
    s = store_with(tmp_path, [mk(1), mk(2)], comments_for=["p1", "p2"])
    s.mark_comments("p1", ok=True, settled=True)
    s.mark_comments("p2", ok=True, settled=True)
    s.mark_complete("comments", "python", "p2")
    s.commit()
    c = FullThreadClient(posts=[mk(1), mk(2), mk(3)])
    result = collect.run(c, s, collect.FetchRequest("python", comments_per_post=0))
    assert sorted(call[1] for call in c.calls if call[0] == "all") == ["p1", "p3"]
    assert not any(call[0] == "comments" for call in c.calls)  # no top-N requests
    assert result.comments == 6 and s.complete_keys("comments", "python") == {"p1", "p2", "p3"}
    c.calls.clear()
    collect.run(c, s, collect.FetchRequest("python", comments_per_post=0))
    assert not any(call[0] in ("all", "comments") for call in c.calls)  # every thread is whole now


def test_all_comments_on_a_thread_cut_short_asks_again_next_time(tmp_path):
    s = store_with(tmp_path, [])
    c = FullThreadClient(whole=False, posts=[mk(1)])
    collect.run(c, s, collect.FetchRequest("python", comments_per_post=0))
    assert s.complete_keys("comments", "python") == set()
    c.calls.clear()
    collect.run(c, s, collect.FetchRequest("python", comments_per_post=0))
    assert [call[1] for call in c.calls if call[0] == "all"] == ["p1"]


def test_all_comments_asks_again_for_threads_still_growing_and_counts_only_new_comments(tmp_path):
    # Live on r/AskReddit: a repeat fetch re-read the unsettled threads and said "168 comments" when 5 were new.
    s = store_with(tmp_path, [])
    c = FullThreadClient(posts=[mk(1, score=None, num_comments=None)])
    first = collect.run(c, s, collect.FetchRequest("python", comments_per_post=0))
    assert first.comments == 3
    assert s.complete_keys("comments_so_far", "python") == {"p1"} and s.complete_keys("comments", "python") == set()
    c.calls.clear()
    again = collect.run(c, s, collect.FetchRequest("python", comments_per_post=0))
    assert [call[1] for call in c.calls if call[0] == "all"] == ["p1"]  # it may have grown, so it's asked again
    assert again.comments == 0  # the same three comments came back: nothing new


def test_a_capped_fill_asks_until_each_thread_has_its_cap_and_a_bigger_cap_tops_it_up(tmp_path):
    s = store_with(tmp_path, [mk(1), mk(2)], comments_for=["p1"])  # p1 has 1 comment saved, p2 none
    s.mark_comments("p1", ok=True, settled=True)
    s.commit()

    class Capped(FullThreadClient):
        def fetch_all_comments(self, post, cap=0, pick="top"):
            self.calls.append(("all", post.id, cap, pick))
            thread = [Comment(f"c{post.id}{j}", post.id, "Python", "t3_" + post.id, "b", "ok", j, T, 0, "u")
                      for j in range(6)]
            return thread[:cap or None], cap == 0 or cap >= 6
    c = Capped(posts=[mk(1), mk(2)])
    req = dict(comments_per_post=3, comment_pick="random", fill_threads=True)
    result = collect.run(c, s, collect.FetchRequest("python", **req))
    assert sorted(call[1:] for call in c.calls if call[0] == "all") == [("p1", 3, "random"), ("p2", 3, "random")]
    assert result.comments == 6 and s.complete_keys("comments", "python") == set()
    c.calls.clear()
    collect.run(c, s, collect.FetchRequest("python", **req))
    assert not [call for call in c.calls if call[0] in ("all", "comments")]  # each has its 3
    bigger = collect.run(c, s, collect.FetchRequest("python", **dict(req, comments_per_post=10)))
    assert bigger.comments == 6 and s.complete_keys("comments", "python") == {"p1", "p2"}  # whole under 10
    c.calls.clear()
    collect.run(c, s, collect.FetchRequest("python", comments_per_post=3))  # the Fetch drawer: asks each post once
    assert not [call for call in c.calls if call[0] in ("all", "comments")]


def test_a_whole_range_gets_each_days_comments_before_the_next_days_posts(tmp_path):
    # Live on r/AskReddit: a week's fetch read ~31k posts before asking for one comment, so Stop in the first
    # minutes left none. Now each day's comments follow its posts, newest day first; quiet days read together have
    # their comments together.
    s = store_with(tmp_path, [])
    c = DayClient(days=3, num_comments=2)
    collect.run(c, s, collect.FetchRequest("python", after=D0, before=D0 + 3 * 86400, limit=None))
    steps = [("posts", (call[1] - D0) // 86400) if call[0] == "posts" else ("comments", int(call[1][1]))
             for call in c.calls if call[0] in ("posts", "comments")]
    assert steps == [("posts", 2)] + [("comments", 2)] * 3 + [("posts", 0)] + [("comments", d) for d in (0, 1)
                                                                               for _ in range(3)]


def test_quiet_days_are_read_together_and_busy_ones_a_day_at_a_time(tmp_path, monkeypatch):
    # A quiet week took four searches a day, whatever the day held. Days in a row are read as one range when the
    # days read so far say they hold about BATCH_POSTS between them; a busy day keeps its own read.
    quiet = DayClient(per_day=3, days=6)
    collect.run(quiet, store_with(tmp_path, []), collect.FetchRequest(
        "python", after=D0, before=D0 + 6 * 86400, limit=None, include_comments=False))
    assert [(a, b) for a, b, *_ in quiet.windows] == [(D0 + 5 * 86400, D0 + 6 * 86400), (D0 + 3 * 86400, D0 + 5 * 86400),
                                                      (D0, D0 + 3 * 86400)]
    # Runs grow a step at a time, at most twice the last one: today, or a day the archive has nothing for yet,
    # reads as quiet even on a busy subreddit, and a month in one run would keep every comment back until it's read.
    month = DayClient(per_day=3, days=40)
    collect.run(month, store_with(tmp_path / "m", []), collect.FetchRequest(
        "python", after=D0, before=D0 + 40 * 86400, limit=None, include_comments=False))
    assert [round((b - a) / 86400) for a, b, *_ in month.windows] == [1, 2, 4, 8, 16, 9]
    monkeypatch.setattr(collect, "BATCH_POSTS", 5)  # so three posts a day is busy: two days would be six
    busy = DayClient(per_day=3, days=3)
    collect.run(busy, store_with(tmp_path / "b", []), collect.FetchRequest(
        "python", after=D0, before=D0 + 3 * 86400, limit=None, include_comments=False))
    assert [(a, b) for a, b, *_ in busy.windows] == [(D0 + d * 86400, D0 + (d + 1) * 86400) for d in (2, 1, 0)]
    monkeypatch.undo()
    # Days that aren't one after another (complete ones between are left out) aren't read together.
    gap = DayClient(per_day=3, days=4)
    s = store_with(tmp_path / "c", [])
    s.mark_complete("posts", "python", str(D0 + 2 * 86400))
    collect.run(gap, s, collect.FetchRequest("python", after=D0, before=D0 + 4 * 86400, limit=None,
                                             include_comments=False))
    assert [(a, b) for a, b, *_ in gap.windows] == [(D0 + 3 * 86400, D0 + 4 * 86400), (D0, D0 + 2 * 86400)]


def test_days_read_together_are_each_complete_unless_part_of_that_day_went_unread(tmp_path):
    # One stretch the archive kept timing out on leaves its own day not complete, not every day read with it.
    class Patchy(DayClient):
        def fetch_posts(self, *a, **kw):
            got = super().fetch_posts(*a, **kw)
            if a[1] == D0 + 86400:  # the run of the two days before the newest: an hour of the older one went unread
                self.exhausted, self.unread = False, [(D0 + 86400 + 7200, D0 + 86400 + 10800)]
            return got
    s = store_with(tmp_path, [])
    collect.run(Patchy(per_day=3, days=4), s, collect.FetchRequest(
        "python", after=D0, before=D0 + 4 * 86400, limit=None, include_comments=False))
    assert s.complete_keys("posts", "python") == {str(D0 + d * 86400) for d in (0, 2, 3)}


def test_a_day_whose_threads_keep_failing_leaves_only_that_days_comments_for_the_next_fetch(tmp_path):
    # Five threads failing in a row used to end comments for the rest of the job: on a year's download, every day
    # after went without, each marked failed in a moment. Failing threads stop their own day; the next tries again.
    s = store_with(tmp_path, [])
    c = DayClient(per_day=collect.GIVE_UP_AFTER + 2, days=3, num_comments=2)
    c.failing = {p.id for p in c.posts if p.id.startswith("d2")}  # the newest day, read first
    result = collect.run(c, s, collect.FetchRequest("python", after=D0, before=D0 + 3 * 86400, limit=None))
    asked = [call[1] for call in c.calls if call[0] == "comments"]
    assert sum(pid.startswith("d2") for pid in asked) == collect.GIVE_UP_AFTER  # that day stopped asking
    assert {p.id for p in c.posts if not p.id.startswith("d2")} <= set(asked)  # the older days were still asked
    assert result.comments == 2 * (collect.GIVE_UP_AFTER + 2)
    failed = {r["id"] for r in s.posts_between("python", None, None) if r["comments_ok"] == 0}
    assert failed == c.failing  # retried on the next fetch

    # The archive failing every thread: GAVE_UP_DAYS days are tried, then none, rather than five more a day.
    s = Store(tmp_path / "all.db")
    c = DayClient(per_day=collect.GIVE_UP_AFTER + 2, days=5, num_comments=2)
    c.failing = {p.id for p in c.posts}
    result = collect.run(c, s, collect.FetchRequest("python", after=D0, before=D0 + 5 * 86400, limit=None))
    assert sum(call[0] == "comments" for call in c.calls) == collect.GAVE_UP_DAYS * collect.GIVE_UP_AFTER
    assert result.comment_errors == len(c.posts)
    assert all(r["comments_ok"] == 0 for r in s.posts_between("python", None, None))


def test_stopping_after_a_day_keeps_its_comments(tmp_path):
    s = store_with(tmp_path, [])
    c = DayClient(days=3, num_comments=2)
    stop = threading.Event()
    real = c.fetch_comments

    def fetch_comments(post, limit, pick="top"):
        got = real(post, limit)
        if post.id == "d2p2":  # the newest day's last post: Stop pressed as its comments come in
            stop.set()
        return got
    c.fetch_comments = fetch_comments
    result = collect.run(c, s, collect.FetchRequest("python", after=D0, before=D0 + 3 * 86400, limit=None),
                         should_stop=stop.is_set)
    assert result.stopped and len(c.windows) == 1
    assert {r["post_id"] for r in s.comments("python")} == {"d2p0", "d2p1", "d2p2"}


def test_posts_a_stopped_day_owed_comments_get_them_from_the_next_fetch(tmp_path):
    # Stop as a day's first thread came in left its other posts never asked about, and the day, read to its end,
    # complete: fetching it again didn't read it, so they never got comments.
    s = store_with(tmp_path, [])
    c = DayClient(days=1, num_comments=2)
    stop = threading.Event()
    real = c.fetch_comments

    def fetch_comments(post, limit, pick="top"):
        stop.set()
        return real(post, limit)
    c.fetch_comments = fetch_comments
    req = dict(after=D0, before=D0 + 86400, limit=None)
    assert collect.run(c, s, collect.FetchRequest("python", **req), should_stop=stop.is_set).stopped
    assert len({r["post_id"] for r in s.comments("python")}) < 3 and str(D0) in s.complete_keys("posts", "python")
    c.fetch_comments = real
    collect.run(c, s, collect.FetchRequest("python", **req, include_comments=False))  # not asked for: none fetched
    assert len({r["post_id"] for r in s.comments("python")}) < 3
    result = collect.run(c, s, collect.FetchRequest("python", **req))
    assert {r["post_id"] for r in s.comments("python")} == {"d0p0", "d0p1", "d0p2"} and not result.comment_errors


class WaitingClient(FakeClient):
    """Its post search has to wait out a pause the archive asked for, as a timeout or a 429 makes it."""

    def __init__(self, posts):
        super().__init__(posts)
        self.limiter = RateLimiter(clock=lambda: 0.0, sleep=lambda secs: None)
        self.seen = []

    def fetch_posts(self, subreddit, after, before, limit, query, top=False, skip=(), progress=None, **kw):
        progress("part 1 of 4")
        self.limiter.hold(4.0, "Arctic Shift is slow right now")
        self.limiter.wait()
        self.seen.append(self.told[-1])  # on screen while the retried search is out
        return super().fetch_posts(subreddit, after, before, limit, query, top, skip, progress, **kw)


def test_the_progress_line_comes_back_once_a_wait_is_over(tmp_path):
    c = WaitingClient([mk(1)])
    c.told = []
    s = store_with(tmp_path, [])
    collect.run(c, s, collect.FetchRequest("python", after=T - 10, before=T + 10, limit=None),
                progress=lambda done, total, msg: c.told.append(msg))
    waited = c.told.index("Arctic Shift is slow right now. Waiting 4 s before the next request…")
    assert c.told[waited + 1] == c.told[waited - 1] and c.told[waited - 1].startswith("Day 1 of 1")
    assert c.seen == [c.told[waited - 1]]  # not the wait's line, while the search goes on


def test_a_fetch_scores_by_the_subreddits_own_words(tmp_path):
    s = Store(tmp_path / "x.db")
    s.save_word("python", "lovely", -1.0)  # the fake archive's comments all say "lovely"
    s.save_word("rust", "title", 1.0)  # another subreddit's words don't count here
    collect.run(FakeClient(posts=[mk(1)]), s, collect.FetchRequest("python", limit=1))
    assert [(r["body"], r["label"]) for r in s.comments("python")] == [("lovely", "negative")]
    assert [r["label"] for r in s.posts("python")] == ["neutral"]


def test_a_days_comments_skip_posts_whose_recheck_finds_nothing_to_download(tmp_path):
    # The owner, 21 Oct: Download all comments "checks posts with empty comments". Posts saved before the archive had
    # counted their comments are re-checked first; the ones that turn out to have none (or only ones already
    # saved) were still asked for their thread, one request each.
    s = store_with(tmp_path, [mk(i, score=None, num_comments=None) for i in (1, 2, 3)], comments_for=("p2",))
    c = FullThreadClient(by_id=[mk(1, score=5, num_comments=0), mk(2, score=5, num_comments=1),
                                mk(3, score=5, num_comments=3)])
    said = []
    collect.run(c, s, collect.FetchRequest("python", comment_posts=("p1", "p2", "p3")),
                progress=lambda done, total, msg: said.append(msg))
    assert [call[1] for call in c.calls if call[0] == "all"] == ["p3"]
    assert "Skipping 2 of 3 posts: no comments to download" in said
    assert {"p1", "p2"} <= s.complete_keys("comments", "python")  # not offered again
    # Check again on one post still asks the archive: its count can lag behind its comments.
    c.calls.clear()
    collect.run(c, s, collect.FetchRequest("python", comment_posts=("p1",)))
    assert [call[1] for call in c.calls if call[0] == "all"] == ["p1"]


def test_a_days_sample_is_enough_for_five_points_and_gives_every_comment_the_same_chance():
    # The textbook size: 1.96² · ¼ / 0.05² = 384.2, so 385 of a big day; the finite population correction asks
    # fewer of a smaller one; a day of no more than 385 is kept whole.
    assert collect.Sample.NEED == 385
    assert collect.Sample({"a": 10 ** 7}).size == 385
    assert collect.Sample({"a": 1000}).size == 278
    small = collect.Sample({"a": 300, "b": 85, "c": None})
    assert small.whole and small.quota("a") is None and small.quota("c") is None
    # One busy thread among many small ones: its quota follows its share of the day's comments, so neither the
    # busy thread nor the small ones are favoured. Over many days, each comment's chance is the same, size / N.
    big_rate, small_rate = [], []
    for day in range(200):
        counts = {f"d{day}big": 3000, **{f"d{day}s{k}": 3 for k in range(1000)}}
        s = collect.Sample(counts)
        quotas = {pid: s.quota(pid) for pid in counts}
        assert sum(quotas.values()) == s.size == 362  # exactly the size: the day's lowest draws
        big_rate.append(quotas[f"d{day}big"] / 3000)
        small_rate.append(sum(q for pid, q in quotas.items() if not pid.endswith("big")) / 3000)
    rate = 362 / 6000
    assert abs(sum(big_rate) / 200 - rate) < 0.004 and abs(sum(small_rate) / 200 - rate) < 0.004
    # The same posts give the same sample.
    assert collect.Sample({"x": 900, "y": 40}).quotas == collect.Sample({"y": 40, "x": 900}).quotas


def test_a_threads_part_follows_its_tree_where_the_archive_counted_it_short():
    s = collect.Sample({"a": 1000, "b": 1000})
    q = s.quota("a")
    tree = [Comment(f"c{j}", "a", "Python", "t3_a", "b", "ok", 1, T, 0, "u") for j in range(1250)]
    kept = s.keep("a", tree)
    assert len(kept) == round(q * 1.25)  # 1,250 in the tree: the same share of it
    assert {c.id for c in kept} == {c.id for c in s.keep("a", list(reversed(tree)))}  # picked by id, not order


def test_coverage_sample_reads_only_the_threads_holding_part_of_it_then_counts_the_day_done(tmp_path):
    from subsentiment.store import SAMPLED
    posts = [mk(i, num_comments=2000 if i == 0 else 4) for i in range(300)]  # 3,196 comments on one day
    s = store_with(tmp_path, posts)

    class Sized(FakeClient):
        def fetch_all_comments(self, post, cap=0, pick="top"):
            self.calls.append(("all", post.id, cap))
            return [Comment(f"c{post.id}_{j}", post.id, "Python", "t3_" + post.id, "b", "ok", 1, T, 0, "u")
                    for j in range(post.num_comments)], True
    c = Sized()
    day = int(T // collect.DAY * collect.DAY)
    ids = tuple(s.open_threads("python", [day], cap=SAMPLED))
    assert len(ids) == 300
    req = collect.FetchRequest("python", comment_posts=ids, fill_threads=True, comment_pick=collect.SAMPLE)
    result = collect.run(c, s, req)
    sample = collect.Sample(s.comment_totals("python", day, day + collect.DAY))
    asked = [call[1] for call in c.calls if call[0] == "all"]
    assert result.comments == sample.size == 344 and len(s.comments("python")) == 344
    assert "p0" in asked and 0 < len(asked) < 150  # the busy thread, and only some of the small ones
    assert all(call[2] == 0 for call in c.calls if call[0] == "all")  # each thread asked for whole, then cut
    assert s.whole_threads_per_day("python", day, day + collect.DAY, SAMPLED) == {day: (300, 300)}
    assert s.whole_threads_per_day("python", day, day + collect.DAY)[day][0] < len(asked)  # sampled, not whole
    assert s.open_threads("python", [day], cap=SAMPLED) == []
    c.calls.clear()
    assert collect.run(c, s, req).comments == 0 and not c.calls  # held already: nothing asked again


class WholeTrees(DayClient):
    """Each thread's tree as big as its post counts (100 where it isn't counted yet)."""

    def fetch_all_comments(self, post, cap=0, pick="top"):
        self.calls.append(("all", post.id, cap))
        return [Comment(f"c{post.id}_{j}", post.id, "Python", "t3_" + post.id, "b", "ok", 1, T, 0, "u")
                for j in range(100 if post.num_comments is None else post.num_comments)], True


def test_a_day_with_posts_not_counted_yet_is_kept_whole_not_sampled_without_them(tmp_path):
    # Half the day's posts aren't counted yet. The sample used to be sized for the counted half alone (278 of 1,000)
    # while the uncounted threads were kept whole: 1,278 saved, 78% of them from half the day, marked a sample.
    from subsentiment.store import SAMPLED
    counted = [mk(i, num_comments=100) for i in range(10)]
    uncounted = [mk(10 + i, score=None, num_comments=None) for i in range(10)]
    s = store_with(tmp_path, counted + uncounted)
    day = int(T // collect.DAY * collect.DAY)
    req = collect.FetchRequest("python", comment_posts=tuple(s.open_threads("python", [day], cap=SAMPLED)),
                               fill_threads=True, comment_pick=collect.SAMPLE)
    collect.run(WholeTrees(), s, req)
    saved = s.comment_counts("python", day, day + collect.DAY)
    assert saved == {p.id: 100 for p in counted + uncounted}  # every comment: its share of the day isn't known yet
    assert not s.complete_keys("sampled", "python")  # and not shown as a sample


def test_a_days_sample_waits_for_the_rest_of_the_day_when_part_of_it_went_unread(tmp_path):
    # The archive timed out on part of the day. Its saved half used to be sampled at its own size and held; once
    # the rest came in, the first half kept its bigger share (27.8% against 7.5%).
    class Patchy(WholeTrees):
        cut = True

        def fetch_posts(self, *a, **kw):
            every, self.posts = self.posts, self.posts[:10] if self.cut else self.posts  # the first 12 hours
            got = super().fetch_posts(*a, **kw)
            self.posts = every
            if self.cut:
                self.exhausted, self.timed_out, self.unread = False, True, [(D0 + 39600, D0 + 86400)]
            else:
                self.timed_out, self.unread = False, []
            return got
    s = store_with(tmp_path, [])
    c = Patchy(per_day=20, days=1, num_comments=100)
    req = collect.FetchRequest("python", after=D0, before=D0 + 86400, limit=None, comment_pick=collect.SAMPLE)
    collect.run(c, s, req)
    assert len(s.posts("python")) == 10
    assert not any(call[0] == "all" for call in c.calls) and not s.comments("python")  # held back, not sampled
    assert not s.complete_keys("sampled", "python")
    c.cut = False
    collect.run(c, s, req)
    sample = collect.Sample(s.comment_totals("python", D0, D0 + 86400))
    saved = s.comment_counts("python", D0, D0 + 86400)
    assert sample.population == 2000 and sum(saved.values()) == sample.size
    assert all(saved.get(p.id, 0) == sample.quota(p.id) for p in c.posts)  # every post its share of the whole day
