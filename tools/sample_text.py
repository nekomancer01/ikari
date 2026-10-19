"""Invented posts and comments for the README screenshots (see tools/screenshot.py).

r/riverton is a made-up city subreddit. Over the sample's weeks the city opens a tram line, it
breaks down for a while, then gets fixed, so the daily mood has a visible dip and recovery.
Titles and comments are built from the parts below so that few of them repeat exactly.
"""
from __future__ import annotations

import random

LINES = ["Green Line", "River Line", "Line 4"]

# Things people post about. "{line}" is filled with a tram line name.
SUBJECTS = [
    "the {line}", "the new library branch", "the farmers market", "Riverside park",
    "the night market", "the dumpling place on Mill Street", "the old bridge", "the new cycle lanes",
    "the swimming pool", "the bakery on Elm Street", "the station cafe",
    "the community garden", "the Saturday flea market", "the harbour walk",
]

# Strong, hand-written headlines. Each is used at most once, so the "strongest" lists don't repeat.
HEADLINES = {
    "positive": [
        "The {line} is honestly the best thing the city has done in years",
        "Shoutout to the kind driver on the {line}, what a lovely person",
        "Riverside park cleanup was a huge success, thanks everyone who came",
        "Found the best dumplings in town on Mill Street, so good",
        "The new library branch is beautiful and the staff are lovely",
        "Sunset over the old bridge tonight was stunning",
        "Love how quiet and smooth the {line} ride is",
        "Farmers market is back and it's wonderful",
        "Thank you to whoever returned my lost wallet, you're amazing",
        "The night market lanterns look gorgeous this year",
        "Great news: the {line} is running on time again",
        "Happy to report my commute is twenty minutes shorter now",
    ],
    "negative": [
        "The {line} broke down again, absolutely miserable hour stuck inside",
        "Third day in a row the {line} is cancelled, this is ridiculous",
        "Disgusting vandalism at the 5th Street bus shelter again",
        "Rent went up 18% and the heating is still broken, I hate this",
        "The {line} app is useless and keeps charging me twice, awful",
        "Worst traffic I've ever seen since they closed the bridge",
        "Terrible experience at the council meeting, nobody listened",
        "Sick of the noise from the construction site at 6am",
        "Bike stolen outside the station, police didn't care",
        "The flooding on Canal Road is awful and nobody is fixing it",
        "Frustrated that the {line} delays still have no explanation",
    ],
    "neutral": [],
}

# Milder everyday titles, so the bulk of posts don't crowd the extremes.
POST_TEMPLATES = {
    "positive": [
        "{S} is decent these days",
        "Nice afternoon at {s}",
        "{S} has improved a lot",
        "Glad they reopened {s}",
        "Enjoying {s} this week",
    ],
    "negative": [
        "Kind of disappointed with {s}",
        "Not impressed with {s} lately",
        "Problems with {s} again",
        "Bad day at {s}",
        "Annoyed about {s} this week",
    ],
    "neutral": [
        "Does {s} open on public holidays?",
        "What are the hours for {s}?",
        "Is there parking near {s}?",
        "Council meeting about {s} on Thursday at 7pm",
        "Updated timetable for {s}",
        "Road closure near {s} from Monday",
        "How do I get to {s} from the station?",
        "Map of {s}",
    ],
}

COMMENT_OPENERS = {
    "positive": ["This is great.", "Love it.", "So glad to hear this.", "Brilliant.",
                 "Thanks for sharing!", "Wonderful news.", "Beautiful.", "Agreed, it's fantastic."],
    "negative": ["This is awful.", "Same here, horrible week.", "Unacceptable.", "So tired of this.",
                 "Worst service ever.", "Pathetic.", "What a mess.", "Ugh, ruined my day too."],
    "neutral": ["It runs every 10 minutes.", "Check the city website.", "Mine came on Tuesday.",
                "It's on the corner of Mill and 3rd.", "Depends on the stop.",
                "The kiosk by the station sells them.", "Room 2 of the town hall.", "Probably next week."],
}
COMMENT_TAILS = ["", "", " I was there yesterday.", " Same on the {line}.", " My neighbour said so too.",
                 " Took the kids along.", " Saw it on the way to work."]


class Titles:
    """Post titles: an unused headline now and then, otherwise a fresh everyday title."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.headlines = {tone: rng.sample(lines, len(lines)) for tone, lines in HEADLINES.items()}
        self.used: set[str] = set()

    def take(self, tone: str) -> str:
        rng = self.rng
        if self.headlines[tone] and rng.random() < 0.3:
            return self.headlines[tone].pop().format(line=rng.choice(LINES))
        for _ in range(20):  # avoid exact repeats while there are combinations left
            subject = rng.choice(SUBJECTS).format(line=rng.choice(LINES))
            title = rng.choice(POST_TEMPLATES[tone]).format(s=subject, S=subject[0].upper() + subject[1:])
            if title not in self.used:
                break
        self.used.add(title)
        return title


def comment(tone: str, rng: random.Random) -> str:
    tail = rng.choice(COMMENT_TAILS).format(line=rng.choice(LINES))
    return rng.choice(COMMENT_OPENERS[tone]) + tail
