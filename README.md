# Subreddit Sentiment

A desktop app (Python + Qt) that pulls posts and comments from a subreddit, scores their
sentiment, and lets you explore and export the results.

## Features

- Fetch from any subreddit by sort (hot/new/top/rising/controversial) and time range, or search within it.
- Optionally fetch comments for each post (full reply trees, "load more" stubs skipped).
- Sentiment scoring with [VADER](https://github.com/cjhutto/vaderSentiment), which is tuned for
  social-media text. Quoted text, code blocks and URLs are stripped before scoring. Labels use the
  standard cut-offs: compound ≥ 0.05 is positive, ≤ −0.05 negative, otherwise neutral.
- Overview tab: summary numbers, sentiment distribution, mean sentiment per day, and the most
  positive/negative items.
- Posts and Comments tabs: sortable, filterable tables. Double-click a row to open it on Reddit.
- Everything is saved to a local SQLite database, so earlier fetches stay available under
  "Saved data". Fetching again updates existing rows. File → Export writes CSV.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/subsentiment          # or: .venv/bin/python -m subsentiment
```

### Reddit access

With no setup, the app uses Reddit's public JSON endpoints. These are slow (about one request every
6 seconds) and Reddit often blocks them from cloud, VPN and datacenter IPs (HTTP 403).

For reliable access, create a **script** app at <https://www.reddit.com/prefs/apps> and enter its
client id and secret under **File → Reddit API settings**. The app uses app-only OAuth (read-only, no
Reddit password) and can make about 100 requests a minute. Note that the secret is stored
unencrypted in Qt's settings file.

## Development

```bash
.venv/bin/pytest
```

Layout:

| Path | Purpose |
|---|---|
| `src/subsentiment/reddit.py` | Reddit client: anonymous or OAuth, pagination, rate limiting, comment trees |
| `src/subsentiment/sentiment.py` | Text cleanup + VADER scoring |
| `src/subsentiment/store.py` | SQLite storage, CSV export, summary helpers |
| `src/subsentiment/collect.py` | One fetch job: fetch → score → store |
| `src/subsentiment/gui/` | Qt main window, charts, table models, background worker, settings |
