"""Reddit data types and subreddit-name helpers shared by the fetcher, storage and UI."""
from __future__ import annotations

import re
from urllib.parse import urlsplit
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
    image: bool = False  # a picture or gallery with no text of its own: skipped, never shown or scored

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


_IMAGE_HOSTS = ("i.redd.it", "preview.redd.it", "i.imgur.com", "imgur.com")
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def is_image_post(url: str | None, selftext: str | None, post_hint: str = "", gallery=False) -> bool:
    """Whether a post is just a picture: it links to an image (Reddit's own or imgur, a file ending in an
    image extension) or a gallery, or the archive says so (``post_hint`` "image", ``gallery``), and has no
    text of its own. A text post with a picture in it, or a link to an article, is not one. Saved posts
    carry only url and selftext, so those two alone must be enough."""
    if (selftext or "").strip():
        return False
    if post_hint == "image" or str(gallery).lower() == "true":  # the archive sometimes sends flags as text
        return True
    parts = urlsplit(url or "")
    host, path = (parts.hostname or "").lower(), parts.path.lower()
    return (host in _IMAGE_HOSTS or path.endswith(_IMAGE_EXTENSIONS)
            or (host.endswith("reddit.com") and path.startswith("/gallery/")))


_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))  # come along with pasted text


def normalize_subreddit(name: str) -> str:
    """Turn "r/Python", "/r/Python/" or a pasted subreddit URL (share links' ?utm_… included) into
    "Python"; "" when nothing is left. A user's profile comes back as "u/name", which subreddit_error
    explains, rather than as the subreddit "user"."""
    name = _SUBREDDIT_URL.sub("", name.translate(_INVISIBLE).strip()).lstrip("/")
    name = re.split(r"[?#]", name, maxsplit=1)[0]
    if "://" in name:
        return name  # a link to another site (a redd.it short link has no subreddit in it)
    head, _, rest = name.partition("/")
    if head.lower() in ("u", "user") and rest.strip("/"):
        return "u/" + rest.split("/", 1)[0]
    if head.lower() == "r":
        head = rest.split("/", 1)[0]
    return head.strip()


def subreddit_error(name: str) -> str | None:
    """Why a normalised name can't be fetched, or None when it looks valid."""
    if not name:
        return "Enter a subreddit name."
    if name.startswith("u/"):
        return f"“{name}” is a user's profile, not a subreddit."
    if "://" in name:
        return "That link doesn't name a subreddit. Enter the subreddit's name, like python."
    if not _SUBREDDIT_NAME.fullmatch(name):
        return f"“{name}” isn't a valid subreddit name: use 2–21 letters, digits or underscores."
    return None
