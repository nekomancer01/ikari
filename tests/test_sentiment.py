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


def test_model_is_bundled_and_scores_plausibly():
    assert sentiment.score_model("I love this, it's amazing!")[1] == "positive"
    assert sentiment.score_model("This is terrible and I hate it.")[1] == "negative"
    assert sentiment.score_model("The meeting is on Tuesday.")[1] == "neutral"
    assert sentiment.score_model("") == (0.0, "neutral")


def test_model_score_range_and_vader_fallback(monkeypatch):
    compound, _ = sentiment.score("what a wonderful day")
    assert -1 <= compound <= 1
    monkeypatch.setattr(sentiment, "score_model", lambda text: None)
    assert sentiment.score("I love this, it's amazing!") == sentiment.score_vader("I love this, it's amazing!")


def test_obvious_sarcasm_flips_positive_to_negative():
    for text in ("Hooray for the cancer! /s",
                 "Oh great now Soda is screaming again just what I needed.",
                 "Gotta love trailing for 59+ minutes, so pathetic",
                 "I wish that were a thing 🙄"):
        compound, label = sentiment.score(text)
        assert label == "negative" and compound < 0, text


def test_sincere_text_is_not_flagged_as_sarcasm():
    for text in ("I love it when cats go all airplane ears", "Thanks a lot!", "You forgot the /s"):
        assert not sentiment.is_sarcastic(sentiment.clean(text)), text
    # a /s inside quoted text is someone else's
    assert sentiment.score("> great stuff /s\n\nThanks, that helped a lot!")[1] == "positive"


def test_sarcasm_with_two_frames_and_an_everyday_complaint():
    # both frames are stripped, and "breaks" counts as a bad situation even though VADER reads it neutral
    text = "Oh great, another update that breaks everything. Just what I needed."
    assert sentiment.is_sarcastic(sentiment.clean(text))
    assert sentiment.score(text)[1] == "negative"
    assert sentiment.score("Gotta love how the game crashes every time I alt-tab")[1] == "negative"
    assert not sentiment.is_sarcastic(sentiment.clean("Oh wow, my go-to for frozen fries"))


def test_a_post_of_many_short_lines_scores_quickly():
    # Quote stripping matched "\s*" across line breaks, so a long post of short lines took seconds.
    import time
    sentiment.score("warm up")  # loads the model, which isn't what's timed
    start = time.perf_counter()
    sentiment.score(" \n" * 20000)  # 40,000 characters, the most a Reddit post holds
    assert time.perf_counter() - start < 0.5  # over a second before
    assert sentiment.score("great point\n> this is awful and terrible\n")[0] > 0  # quotes still ignored


def test_a_subreddits_own_word_meanings_change_its_labels():
    linux = sentiment.Lexicon({"kill": 0.0, "Big  Shot": 1.0})
    assert sentiment.score("kill the process and restart it")[1] == "negative"  # its usual meaning
    assert linux.score("kill the process and restart it")[1] == "neutral"  # means nothing here
    assert sentiment.Lexicon({"kill": 0.6}).score("kill the process and restart it")[1] == "positive"
    assert linux.score("Spamton is a BIG SHOT!")[1] == "positive"  # phrases, any case and spacing
    assert linux.score("he is not a big shot")[1] == "negative"  # "not" still turns it round
    assert linux.score("he is not a big shot", "vader")[1] == "negative"


def test_text_without_a_set_word_scores_as_before():
    lex = sentiment.Lexicon({"kill": 1.0, "shot": -1.0})
    for text in ("I love this, it's amazing!", "This is terrible and I hate it.", "killer app", "big shots", ""):
        assert lex.score(text) == sentiment.score(text)
        assert lex.score(text, "vader") == sentiment.score(text, "vader")
    assert sentiment.Lexicon().score("kill it") == sentiment.score("kill it")


def test_set_words_count_inside_sarcasm_and_odd_letters_too():
    lex = sentiment.Lexicon({"kill": 1.0, "stuff": 1.0})
    assert lex.score("gotta love when you kill the process")[1] == "positive"  # not sarcasm: "kill" is good here
    assert lex.score("gotta love when the game crashes")[1] == "negative"  # still sarcasm
    assert lex.score("this ſtuff is here")[1] == "positive"  # the search's case folding finds "stuff"
