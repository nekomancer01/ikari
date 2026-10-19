<div align="center">

<img src="src/subsentiment/gui/assets/app-512.png" width="112" alt="ikari icon">

<h1>ikari</h1>

<p><b>Read the mood of any subreddit.</b><br>
Pull posts and comments, score their sentiment offline, and explore the results in a desktop app
for Windows, macOS and Linux.</p>

<p>
  <a href="https://github.com/nekomancer01/ikari/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/nekomancer01/ikari?style=flat-square&color=6C5CE7"></a>
  <a href="https://github.com/nekomancer01/ikari/actions/workflows/build.yml"><img alt="Build status" src="https://img.shields.io/github/actions/workflow/status/nekomancer01/ikari/build.yml?branch=main&style=flat-square&label=build"></a>
  <img alt="Windows, macOS and Linux" src="https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-555?style=flat-square">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="Built with Qt" src="https://img.shields.io/badge/Qt-PySide6-41CD52?style=flat-square&logo=qt&logoColor=white">
</p>

<p>
  <a href="https://github.com/nekomancer01/ikari/releases/latest"><img alt="Download the latest release" src="https://img.shields.io/badge/Download-latest%20release-6C5CE7?style=for-the-badge&logo=github&logoColor=white"></a>
</p>

<p>
  <a href="#features">Features</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#download">Download</a> ·
  <a href="#run-from-source">Run from source</a> ·
  <a href="#reddit-access">Reddit access</a> ·
  <a href="#development">Development</a>
</p>

<br>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/overview-dark.png">
  <img alt="Overview tab: summary tiles, sentiment distribution, mean sentiment per day with a dip and recovery, and the most positive and negative posts" src="docs/overview-light.png" width="100%">
</picture>

</div>

<table>
  <tr>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/posts-dark.png">
        <img alt="Posts tab: a sortable, filterable table of posts with sentiment scores and labels" src="docs/posts-light.png">
      </picture>
      <p align="center"><sub><b>Posts</b>: sort, filter by text or label, double-click to open on Reddit</sub></p>
    </td>
    <td width="50%" valign="top">
      <picture>
        <source media="(prefers-color-scheme: dark)" srcset="docs/comments-dark.png">
        <img alt="Comments tab: every comment with its score, label and the post it belongs to" src="docs/comments-light.png">
      </picture>
      <p align="center"><sub><b>Comments</b>: every reply, scored, with the post it belongs to</sub></p>
    </td>
  </tr>
</table>

<p align="center"><sub>Screenshots follow your GitHub theme. They show invented sample data for a made-up city,
r/riverton, whose new tram line breaks down for a week. Regenerate them with
<code>QT_QPA_PLATFORM=offscreen python tools/screenshot.py</code>.</sub></p>

## Features

- 📥 **Fetch** any subreddit by sort (hot, new, top, rising, controversial) and time range, or
  search within it. Comments are optional and come with full reply trees.
- 🧠 **Score sentiment offline** with a small bundled classifier (TF-IDF plus
  [VADER](https://github.com/cjhutto/vaderSentiment) features) trained on GoEmotions Reddit
  comments. Nothing leaves your machine for scoring.
- 📊 **See the overview**: summary tiles, the sentiment split, mean sentiment per day, and the
  most positive and negative items.
- 🔎 **Browse posts and comments** in sortable, filterable tables. Double-click a row to open it
  on Reddit.
- 💾 **Keep your data** in a local SQLite database. Fetching again updates existing rows, and
  File → Export writes CSV.
- 🌗 **Pick a theme**: the glass interface comes in light and dark (View menu, or follow the
  system). **Reduce transparency** makes every panel opaque.
- 🔄 **Stay up to date**: Help → Check for updates… finds a newer release on GitHub, downloads it
  and hands off to the installer. It also checks at startup, which you can turn off.

### How accurate is it?

The classifier is tuned for accurate totals over many comments rather than every single one.
On the GoEmotions test split:

| | Labels right | Error in the overall positive / neutral / negative split |
|---|:-:|:-:|
| **ikari's model** | **73%** | **4 points** |
| Plain VADER | 65% | 14 points |

Quoted text, code blocks and URLs are stripped before scoring. Plain VADER is the fallback if the
model is missing. Reproduce the numbers with `tools/eval_sentiment.py`.

## How it works

```mermaid
flowchart LR
    A["Reddit<br/>public JSON or OAuth"] --> B["Fetch<br/>posts + comment trees"]
    B --> C["Clean<br/>strip quotes, code, URLs"]
    C --> D["Score<br/>bundled classifier"]
    D --> E[("SQLite<br/>on your machine")]
    E --> F["Overview, Posts,<br/>Comments, CSV"]
```

## Download

Ready-made builds for Windows, macOS and Linux are attached to each
[release](https://github.com/nekomancer01/ikari/releases). Builds of the latest `main` are on the
[Actions tab](https://github.com/nekomancer01/ikari/actions/workflows/build.yml) (open a run, then
*Artifacts*).

| OS | Download | How to run |
|---|---|---|
| Windows 10/11 (x64) | `ikari-Setup-<version>.exe` | Run the installer. Or unzip `ikari-<version>-windows-x64.zip` and run `ikari.exe`. |
| macOS 11+ (Apple Silicon) | `ikari-<version>-macos-arm64.dmg` | Open the .dmg and drag the app to Applications. |
| macOS 11+ (Intel) | `ikari-<version>-macos-x86_64.dmg` | As above. |
| Linux (x86_64) | `ikari-<version>-linux-x86_64.AppImage` | `chmod +x` it, then run it. Or unpack the `.tar.gz` and run `./install.sh` to add it to your app menu (`./install.sh --uninstall` removes it). |

> [!NOTE]
> The builds aren't code-signed, so the first launch shows a warning.
>
> - **Windows** SmartScreen ("Windows protected your PC"): choose **More info → Run anyway**.
> - **macOS** Gatekeeper ("can't be opened because Apple cannot check it"): right-click the app,
>   choose **Open**, then **Open** again. On macOS 15, if there's no Open button, go to
>   **System Settings → Privacy & Security** and click **Open Anyway**.
> - **Linux**: the AppImage needs FUSE 2 (`sudo apt install libfuse2` on Ubuntu 22.04,
>   `libfuse2t64` on 24.04), or run it with `--appimage-extract-and-run`. Qt also needs
>   `libxcb-cursor0` on X11, which some minimal installs lack.

## Run from source

Install Python 3.10 or newer, then:

- **Windows:** double-click `run-windows.bat` (tick *Add python.exe to PATH* when installing
  Python from [python.org](https://www.python.org/downloads/)).
- **macOS:** double-click `run-macos.command`, or run `./run.sh` in Terminal.
- **Linux:** run `./run.sh`.

The first run sets up a private environment in `.venv` (about a minute); after that it opens
straight away. Delete `.venv` to reinstall.

## Build the app yourself

<details>
<summary>Build commands and what ends up in <code>dist/</code></summary>

Builds are made on the OS they're for. In a Python environment:

```bash
pip install . pyinstaller
python packaging/build.py        # Linux: add --no-appimage to skip the AppImage
```

On Windows you can instead double-click `packaging\build-windows.bat`. Everything lands in
`dist/`: the zip and installer on Windows (the installer needs
[Inno Setup 6](https://jrsoftware.org/isinfo.php)), the `.app` and `.dmg` on macOS, and the
AppImage and `.tar.gz` on Linux. (The folder keeps the
app's former name so existing data and saved Reddit secrets carry over.)

The workflow in `.github/workflows/build.yml` builds and launch-checks all four targets on every
push to `main` and every pull request. Pushing a `v*` tag also publishes a GitHub release with
the packages attached. The version comes from `__version__` in `src/subsentiment/__init__.py`.

</details>

## Platform features

<details>
<summary>What ikari does differently on each OS, and where it keeps its data</summary>

- **macOS:** Settings, About and Quit sit in the app menu with the standard shortcuts (⌘, and ⌘Q).
- **macOS:** the title bar's light or dark look follows the app's theme. Optional "macOS vibrancy"
  background (View → Window background) gives a real frosted blur; off by default, applies on next
  launch.
- **Linux:** registers as `io.github.nekomancer01.ikari`, so docks and Wayland
  compositors show the right icon and name.
- **Linux:** "Match system theme" also works on desktops that don't report dark mode to Qt, by
  reading the GNOME/GTK setting.
- **Windows:** title bar matches the theme; optional Mica or Acrylic background on Windows 11
  22H2+; taskbar shows the app's own icon.
- **All platforms:** the API secret goes in the OS keychain, falling back to app settings only if
  none is available.

Data lives in the OS's app-data folder: `%APPDATA%\subsentiment\Subreddit Sentiment\` on
Windows, `~/Library/Application Support/subsentiment/Subreddit Sentiment/` on macOS, and
`~/.local/share/subsentiment/Subreddit Sentiment/` on Linux. The folder keeps the
app's former name so existing data and saved Reddit secrets carry over.

</details>

## Reddit access

With no setup, the app reads from [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift), a
free, community-run archive of Reddit posts and comments. It needs no account or key. Reddit stopped
serving its public JSON endpoints to logged-out requests in 2026, so the app no longer uses them.

Things to know about the archive:

- Results come in date order. **new**, **hot** and **rising** all mean newest first; **top** and
  **controversial** take the newest posts in the chosen time range and rank them.
- New posts appear quickly, but scores and comment counts only settle after about 36 hours.
- It's a volunteer service with no uptime guarantee. The app sends at most one request a second.

If Reddit has approved a **script** app of yours under its
[Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy),
enter its client id and secret under **File → Reddit API settings** and the app reads Reddit
directly instead (app-only OAuth, read-only, about 100 requests a minute). Reddit has announced that
this public API closes in March 2027. The secret goes into the system credential store (Windows
Credential Manager, macOS Keychain, or Secret Service on Linux); only if none is available is it
kept in the app's settings unencrypted.

## Design notes

<details>
<summary>Type, glass and chart colors</summary>

- Type: [Manrope](https://fonts.google.com/specimen/Manrope) for the interface and
  [Sora](https://fonts.google.com/specimen/Sora) for headings and big numbers. Tabular figures are
  on so numbers line up. Qt's variable-font weight matching is unreliable, so the fonts are bundled
  as static weights in `src/subsentiment/gui/assets/`.
- Glass is used for the sidebar, stat tiles and chart cards. Tables and lists sit on near-solid
  panels so dense text stays readable, and glass is never stacked on glass.
- Chart colors: blue for positive, red for negative, gray for neutral, checked for color-blind
  separation. Bars are always labelled, so color is never the only cue.

### Licenses

- Manrope and Sora are licensed under the [SIL Open Font License 1.1](https://openfontlicense.org/).
  Their license texts ship with the app as `src/subsentiment/gui/assets/Manrope-OFL.txt` and
  `src/subsentiment/gui/assets/Sora-OFL.txt`.

</details>

## Development

```bash
.venv/bin/pytest
```

Layout:

| Path | Purpose |
|---|---|
| `src/subsentiment/reddit.py` | Reddit client: anonymous or OAuth, pagination, rate limiting, comment trees |
| `src/subsentiment/arctic_shift.py` | Arctic Shift archive client (the default source) |
| `src/subsentiment/sources.py` | Picks Reddit's API or Arctic Shift from the saved settings |
| `src/subsentiment/sentiment.py` | Text cleanup + scoring (bundled model, VADER fallback) |
| `src/subsentiment/sentiment_model.py` | Loads the bundled classifier and its feature code |
| `src/subsentiment/store.py` | SQLite storage, CSV export, summary helpers |
| `src/subsentiment/updater.py` | Looks up the latest GitHub release and downloads it |
| `tools/train_model.py` | Trains the bundled sentiment classifier on GoEmotions |
| `tools/eval_sentiment.py` | Scores accuracy against GoEmotions (the numbers in Features) |
| `tools/screenshot.py` | Renders the README screenshots in `docs/` from invented sample data |
| `tools/sample_text.py` | The invented r/riverton posts and comments the screenshots use |
| `src/subsentiment/collect.py` | One fetch job: fetch → score → store |
| `src/subsentiment/gui/` | Qt main window, charts, table models, background worker, settings |
| `src/subsentiment/gui/theme.py` | Glass theme: color tokens, stylesheet, fonts, backdrop |
| `src/subsentiment/gui/native/` | Per-OS integration: Windows title bar and Mica/Acrylic, macOS menus and vibrancy, Linux desktop id and theme |
| `packaging/build.py` | One build entry for all platforms (PyInstaller, then zip/installer, .dmg, or AppImage/.tar.gz) |
| `packaging/` | PyInstaller spec, Inno Setup installer, Linux AppRun/.desktop/install.sh |
| `tools/make_icon.py` | Regenerates the app icons (needs Pillow) |
