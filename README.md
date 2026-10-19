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
- Glass interface: translucent panels over a soft gradient, light and dark themes (View menu, or
  follow the system), and a **Reduce transparency** option that makes every panel opaque.
- Everything is saved to a local SQLite database, so earlier fetches stay available under
  "Saved data". Fetching again updates existing rows. File → Export writes CSV.

## Windows

**Easiest: run from source.** Install Python 3.10 or newer from
[python.org](https://www.python.org/downloads/) (tick *Add python.exe to PATH*), then double-click
`run-windows.bat`. The first run sets up a private environment in `.venv` (about a minute); after
that it opens straight away. Delete `.venv` to reinstall.

**Build a standalone app (.exe).** On a Windows machine with Python, double-click
`packaging\build-windows.bat`. It produces:

- `dist\SubredditSentiment\SubredditSentiment.exe`, a folder you can copy anywhere and run
- `dist\SubredditSentiment-windows-portable.zip`, the same folder zipped
- `dist\SubredditSentiment-Setup-0.2.0.exe`, an installer with a Start menu entry (only if
  [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed)

The GitHub Actions workflow in `.github/workflows/windows-build.yml` does the same on every push
to `main` and attaches the zip and installer to the run.

The app isn't code-signed, so Windows SmartScreen will warn the first time ("Windows protected your
PC"). Choose **More info → Run anyway**.

**Windows-specific features**

- The title bar follows the app's light or dark theme and, on Windows 11, takes the window's color.
- **View → Window background → Windows Acrylic** or **Windows Mica** uses the real Windows 11
  frosted-glass material behind the app (Windows 11 22H2 or later; takes effect after a restart).
  Acrylic blurs whatever is behind the window; Mica tints with your wallpaper.
- Your Reddit API secret is stored in Windows Credential Manager, not in a plain settings file.
- The taskbar shows the app's own icon.
- Data lives in `%APPDATA%\subsentiment\Subreddit Sentiment\subsentiment.db`.

## macOS / Linux

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
Reddit password) and can make about 100 requests a minute. The secret goes into the system
credential store (Windows Credential Manager, macOS Keychain, or Secret Service on Linux); only if
none is available is it kept in the app's settings unencrypted.

## Design notes

- Type: [Manrope](https://fonts.google.com/specimen/Manrope) for the interface and
  [Sora](https://fonts.google.com/specimen/Sora) for headings and big numbers. Both are SIL Open Font
  License fonts, bundled in `src/subsentiment/gui/assets/` as static weights cut from the variable
  fonts (Qt's variable-font weight matching is unreliable). Tabular figures are on so numbers line up.
- Glass is used for the sidebar, stat tiles and chart cards. Tables and lists sit on near-solid
  panels so dense text stays readable, and glass is never stacked on glass.
- Chart colors: blue for positive, red for negative, gray for neutral, checked for color-blind
  separation. Bars are always labelled, so color is never the only cue.

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
| `src/subsentiment/gui/theme.py` | Glass theme: color tokens, stylesheet, fonts, backdrop |
| `src/subsentiment/gui/win_native.py` | Windows title bar, Mica/Acrylic, taskbar identity |
| `packaging/` | PyInstaller spec, Windows build script, Inno Setup installer |
| `tools/make_icon.py` | Regenerates the app icon (needs Pillow) |
