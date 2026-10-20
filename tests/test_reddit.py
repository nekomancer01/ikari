import pytest

from subsentiment.reddit import normalize_subreddit, subreddit_error


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
