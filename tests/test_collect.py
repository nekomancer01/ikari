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
                    should_stop=None):
        self.calls.append(("posts", top, set(skip)))
        return [p for p in self.posts if top or p.id not in skip][:limit]

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
    result = collect.run(c, s, collect.FetchRequest("python"))
    assert c.calls[0] == ("ids", ["p1"])
    assert {r["id"]: r["score"] for r in s.posts("python")} == {"p1": 77, "p2": 10}
    assert result.updated == 1 and result.posts == 0
    assert ("comments", "p1") in c.calls and ("comments", "p2") not in c.calls


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
