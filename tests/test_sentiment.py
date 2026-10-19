from subsentiment import sentiment


def test_polarity():
    assert sentiment.score("I love this, it's amazing!")[1] == "positive"
    assert sentiment.score("This is terrible and I hate it.")[1] == "negative"
    assert sentiment.score("The meeting is on Tuesday.")[1] == "neutral"


def test_empty_and_link_only_text_is_neutral():
    assert sentiment.score("") == (0.0, "neutral")
    assert sentiment.score("https://example.com/awesome-great-love") == (0.0, "neutral")


def test_quotes_and_code_are_ignored():
    text = "> I hate everything about this\n\n```\nterrible_error()\n```\nThanks, that helped a lot!"
    assert sentiment.clean(text) == "Thanks, that helped a lot!"
    assert sentiment.score(text)[1] == "positive"


def test_markdown_links_keep_their_text():
    assert sentiment.clean("see [the great docs](https://x.y)") == "see the great docs"
