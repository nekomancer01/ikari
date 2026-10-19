"""The fetch job: what gets refreshed, what gets skipped, and how comment failures are handled."""
from subsentiment import collect
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
                    should_stop=None, on_posts=None):
        self.calls.append(("posts", top, set(skip)))
        picked = [p for p in self.posts if top or p.id not in skip][:limit]
        for p in picked:  # the real client hands over each page as it is downloaded
            if on_posts:
                on_posts([p])
        return picked

    def fetch_comments(self, post, limit):
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


def test_posts_known_to_have_no_comments_are_not_looked_up(tmp_path):
    s = store_with(tmp_path, [])
    c = FakeClient(posts=[mk(1, num_comments=0), mk(2, score=None, num_comments=None)])
    collect.run(c, s, collect.FetchRequest("python"))
    assert [call[1] for call in c.calls if call[0] == "comments"] == ["p2"]


def test_a_post_whose_comments_came_back_empty_isnt_asked_again(tmp_path):
    s = store_with(tmp_path, [])

    class Quiet(FakeClient):
        def fetch_comments(self, post, limit):
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

    def fetch_all_comments(self, post):
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


def test_downloading_a_whole_day_marks_it_complete_only_when_the_archive_ran_out(tmp_path):
    s = store_with(tmp_path, [mk(1)])
    c = FakeClient(posts=[mk(1), mk(2), mk(3)])
    collect.run(c, s, collect.FetchRequest("python", after=T, before=T + 86400, limit=5, everything=True,
                                           include_comments=False))
    assert s.complete_keys("posts", "python") == {str(int(T))}

    s2 = store_with(tmp_path / "b", [])
    collect.run(FakeClient(posts=[mk(1), mk(2), mk(3)]), s2,
                collect.FetchRequest("python", after=T, before=T + 86400, limit=2, everything=True,
                                     include_comments=False))
    assert s2.complete_keys("posts", "python") == set()  # it may have had more than the 2 asked for


def test_each_post_and_comment_is_saved_and_announced_as_it_arrives(tmp_path):
    s = store_with(tmp_path, [])
    counts = []
    collect.run(FakeClient(posts=[mk(1), mk(2), mk(3)]), s, collect.FetchRequest("python"),
                saved=lambda: counts.append(len(s.posts("python"))))
    assert counts[:3] == [1, 2, 3]  # the table grows one post at a time


def test_a_fetch_does_not_download_comments_for_saved_posts_nobody_asked_about(tmp_path):
    s = store_with(tmp_path, [mk(1), mk(2)])  # saved earlier, comments never requested
    c = FakeClient(posts=[mk(3)])
    collect.run(c, s, collect.FetchRequest("python"))
    assert [call[1] for call in c.calls if call[0] == "comments"] == ["p3"]
