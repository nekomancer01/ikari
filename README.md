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

## Download

Ready-made builds for Windows, macOS and Linux are attached to each
[release](https://github.com/nekomancer01/ikari/releases). Builds of the latest `main` are on the
[Actions tab](https://github.com/nekomancer01/ikari/actions/workflows/build.yml) (open a run, then
*Artifacts*).

| OS | Download | How to run |
|---|---|---|
| Windows 10/11 (x64) | `SubredditSentiment-Setup-<version>.exe` | Run the installer. Or unzip `SubredditSentiment-<version>-windows-x64.zip` and run `SubredditSentiment.exe`. |
| macOS 11+ (Apple Silicon) | `SubredditSentiment-<version>-macos-arm64.dmg` | Open the .dmg and drag the app to Applications. |
| macOS 11+ (Intel) | `SubredditSentiment-<version>-macos-x86_64.dmg` | As above. |
| Linux (x86_64) | `SubredditSentiment-<version>-linux-x86_64.AppImage` | `chmod +x` it, then run it. Or unpack the `.tar.gz` and run `./install.sh` to add it to your app menu (`./install.sh --uninstall` removes it). |

The builds aren't code-signed, so the first launch shows a warning:

- **Windows** SmartScreen ("Windows protected your PC"): choose **More info → Run anyway**.
- **macOS** Gatekeeper ("can't be opened because Apple cannot check it"): right-click the app,
  choose **Open**, then **Open** again. On macOS 15, if there's no Open button, go to
  **System Settings → Privacy & Security** and click **Open Anyway**.
- **Linux**: the AppImage needs FUSE 2 (`sudo apt install libfuse2` on Ubuntu 22.04,
  `libfuse2t64` on 24.04), or run it with `--appimage-extract-and-run`. Qt also needs
  `libxcb-cursor0` on X11, which some minimal installs lack.

## Run from source

Install Python 3.10 or newer, then:

- **Windows:** double-click `run-windows.bat` (tick *Add python.exe to PATH* when installing
  Python from [python.org](https://www.python.org/downloads/)).
- **macOS:** double-click `run-macos.command`, or run `./run.sh` in Terminal.
- **Linux:** run `./run.sh`.

The first run sets up a private environment in `.venv` (about a minute); after that it opens
straight away. Delete `.venv` to reinstall.

## Build the app yourself

Builds are made on the OS they're for. In a Python environment:

```bash
pip install . pyinstaller
python packaging/build.py        # Linux: add --no-appimage to skip the AppImage
```

On Windows you can instead double-click `packaging\build-windows.bat`. Everything lands in
`dist/`: the zip and installer on Windows (the installer needs
[Inno Setup 6](https://jrsoftware.org/isinfo.php)), the `.app` and `.dmg` on macOS, and the
AppImage and `.tar.gz` on Linux.

The workflow in `.github/workflows/build.yml` builds and launch-checks all four targets on every
push to `main` and every pull request. Pushing a `v*` tag also publishes a GitHub release with
the packages attached. The version comes from `__version__` in `src/subsentiment/__init__.py`.

## Platform features

- **macOS:** Settings, About and Quit sit in the app menu with the standard shortcuts (⌘, and ⌘Q).
- **macOS:** the title bar's light or dark look follows the app's theme. Optional "macOS vibrancy"
  background (View → Window background) gives a real frosted blur; off by default, applies on next
  launch.
- **Linux:** registers as `io.github.subsentiment.SubredditSentiment`, so docks and Wayland
  compositors show the right icon and name.
- **Linux:** "Match system theme" also works on desktops that don't report dark mode to Qt, by
  reading the GNOME/GTK setting.
- **Windows:** title bar matches the theme; optional Mica or Acrylic background on Windows 11
  22H2+; taskbar shows the app's own icon.
- **All platforms:** the API secret goes in the OS keychain, falling back to app settings only if
  none is available.

Data lives in the OS's app-data folder: `%APPDATA%\subsentiment\Subreddit Sentiment\` on
Windows, `~/Library/Application Support/subsentiment/Subreddit Sentiment/` on macOS, and
`~/.local/share/subsentiment/Subreddit Sentiment/` on Linux.

## Reddit access

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
| `src/subsentiment/gui/native/` | Per-OS integration: Windows title bar and Mica/Acrylic, macOS menus and vibrancy, Linux desktop id and theme |
| `packaging/build.py` | One build entry for all platforms (PyInstaller, then zip/installer, .dmg, or AppImage/.tar.gz) |
| `packaging/` | PyInstaller spec, Inno Setup installer, Linux AppRun/.desktop/install.sh |
| `tools/make_icon.py` | Regenerates the app icons (needs Pillow) |
