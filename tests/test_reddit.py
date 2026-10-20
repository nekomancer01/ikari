import pytest

from subsentiment.reddit import is_image_post, normalize_subreddit, subreddit_error


@pytest.mark.parametrize("name", ["r/Python", "/r/Python", "/r/Python/", " Python ", "Python/", "R/Python",
                                  "https://www.reddit.com/r/Python/", "old.reddit.com/r/Python/top/?t=week"])
def test_subreddit_name_is_normalised(name):
    assert normalize_subreddit(name) == "Python"


@pytest.mark.parametrize("name", ["python dev", "a", "py-thon", "x" * 22])
def test_invalid_subreddit_names_are_explained(name):
    assert "isn't a valid subreddit name" in subreddit_error(name)


def test_blank_subreddit_asks_for_a_name():
    assert subreddit_error(normalize_subreddit("  r/ ")) == "Enter a subreddit name."
    assert subreddit_error("Python") is None


@pytest.mark.parametrize("name", ["https://www.reddit.com/r/Python?utm_source=share&utm_medium=android_app",
                                  "reddit.com/r/Python#about", "\u200bPython", "Python\ufeff",
                                  "https://www.reddit.com/r/Python/s/AbC123"])
def test_pasted_share_links_and_invisible_characters_still_name_the_subreddit(name):
    assert normalize_subreddit(name) == "Python"


@pytest.mark.parametrize("link", ["https://www.reddit.com/user/spez/", "reddit.com/u/spez"])
def test_a_users_profile_is_explained_not_fetched_as_a_subreddit(link):
    # It used to become "user" (a real subreddit) and was fetched without a word.
    assert subreddit_error(normalize_subreddit(link)) == "“u/spez” is a user's profile, not a subreddit."


def test_a_link_to_another_site_is_explained():
    assert "doesn't name a subreddit" in subreddit_error(normalize_subreddit("https://redd.it/abc123"))


@pytest.mark.parametrize("url, text, hint, gallery, image", [
    ("https://i.redd.it/abc.jpeg", "", "image", False, True),
    ("https://i.redd.it/abc.jpeg", "", "", False, True),  # the archive often has no post_hint
    ("https://i.redd.it/abc.jpeg", "", "link", False, True),
    ("https://i.imgur.com/x.png", "", "", False, True),
    ("https://imgur.com/a/xyz", "", "link", False, True),
    ("https://example.com/pic.WEBP?w=1", "", "", False, True),
    ("https://www.reddit.com/gallery/abc", "", "", True, True),
    ("https://www.reddit.com/gallery/abc", "", "", False, True),
    ("", "", "", True, True),
    ("", "", "", "True", True),
    ("https://i.redd.it/abc.jpeg", "what do you think of my desk setup?", "image", False, False),  # real text stays
    ("https://i.redd.it/abc.jpeg", "   ", "image", False, True),
    ("https://www.reddit.com/r/Python/comments/p1/x/", "", "self", False, False),  # a text post with no body
    ("https://apnews.com/article/1", "", "link", False, False),
    ("https://v.redd.it/abc", "", "hosted:video", False, False),  # videos aren't image posts
    ("", "", "", False, False),
    (None, None, "", None, False),
])
def test_is_image_post(url, text, hint, gallery, image):
    assert is_image_post(url, text, hint, gallery) is image
