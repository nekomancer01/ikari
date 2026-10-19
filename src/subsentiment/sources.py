"""Picks where a fetch reads from: Reddit's API with the user's own app keys, or Arctic Shift."""
from __future__ import annotations

from typing import Protocol

from subsentiment.arctic_shift import ArcticShiftClient
from subsentiment.reddit import Comment, Post, ProgressFn, RedditClient, StopFn


class Source(Protocol):
    authenticated: bool

    def fetch_posts(self, subreddit: str, sort: str = "hot", time_filter: str = "week", limit: int = 100,
                    query: str = "", progress: ProgressFn | None = None,
                    should_stop: StopFn | None = None) -> list[Post]: ...

    def fetch_comments(self, post: Post, limit: int = 100, sort: str = "top") -> list[Comment]: ...


def make_client(credentials: dict) -> Source:
    """Reddit's API when both app keys are set; otherwise Arctic Shift, since Reddit now refuses
    logged-out requests."""
    if credentials.get("client_id") and credentials.get("client_secret"):
        return RedditClient(**credentials)
    return ArcticShiftClient(user_agent=credentials.get("user_agent") or "")
