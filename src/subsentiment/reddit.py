"""Reddit data types and subreddit-name helpers shared by the fetcher, storage and UI."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

_SUBREDDIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{1,20}")
_SUBREDDIT_URL = re.compile(r"^(?:https?://)?(?:[a-z]+\.)?reddit\.com/", re.IGNORECASE)

ProgressFn = Callable[[str], None]
StopFn = Callable[[], bool]


class FetchError(Exception):
    """A fetch failed in a way worth showing the user as is."""


@dataclass
class Post:
    id: str
    subreddit: str
    title: str
    selftext: str
    author: str
    score: int | None  # None until the archive's numbers have settled
    upvote_ratio: float | None
    num_comments: int | None
    created_utc: float
    permalink: str
    url: str

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.selftext}".strip()


@dataclass
class Comment:
    id: str
    post_id: str
    subreddit: str
    parent_id: str
    author: str
    body: str
    score: int | None
    created_utc: float
    depth: int
    permalink: str


def normalize_subreddit(name: str) -> str:
    """Turn "r/Python", "/r/Python/" or a pasted subreddit URL into "Python"; "" when nothing is left."""
    name = _SUBREDDIT_URL.sub("", name.strip()).lstrip("/")
    if name.lower().startswith("r/"):
        name = name[2:]
    return name.split("/", 1)[0].strip()


def subreddit_error(name: str) -> str | None:
    """Why a normalised name can't be fetched, or None when it looks valid."""
    if not name:
        return "Enter a subreddit name."
    if not _SUBREDDIT_NAME.fullmatch(name):
        return f"“{name}” isn't a valid subreddit name: use 2–21 letters, digits or underscores."
    return None
