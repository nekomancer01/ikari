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
