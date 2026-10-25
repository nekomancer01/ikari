---
name: run-ikari
description: Run, launch, drive or screenshot the ikari desktop app (PySide6, package subsentiment) headless. Use to see a GUI change in the real window, fetch from a fake Arctic Shift, take before/after screenshots at 1920x1200 150%, or call sentiment scoring directly.
---

# Run ikari

ikari is a PySide6 window. Headless, you drive it with `driver.py`: it opens the real `MainWindow` (real fetch
threads, HTTP client and SQLite store) against `tests/fake_archive.py` on localhost, with a throwaway database and
settings, on a fake 1920x1200 screen at 150%. It reads one command per line from stdin and saves screenshots.
It never touches the real Arctic Shift or your own data folder.

Paths are relative to the repo root.

## Prerequisites

The session-start hook (`tools/agent_setup.py`) installs the Python packages and Qt's system libraries. If it
didn't run or the image is older than a dependency (`No module named ruff` / `hypothesis`), run it yourself:

```bash
CLAUDE_CODE_REMOTE=true python tools/agent_setup.py
```

No build step: everything runs from `src/` (the driver puts it on `sys.path`).

## Run (agent path): the driver

```bash
printf 'fetch python week\nss overview\npage coverage\nss coverage\nrequests\nquit\n' \
  | python .claude/skills/run-ikari/driver.py
```

Prints `ready: ...`, then `ok` after each command. Screenshots land in `.claude/skills/run-ikari/shots/`
(git-ignored; `--out DIR` to change). The whole run above takes about 6 s.

Commands (full list at the top of `driver.py`):

| command | does |
|---|---|
| `fetch SUB [day\|week\|month] [nocomments]` | fills the Fetch drawer, clicks Fetch, waits until done, prints the result line |
| `page overview\|words\|posts\|comments\|coverage` | switches rail page and waits for its reads |
| `theme light\|dark`, `max`, `restore`, `size W H` | window state (sizes are logical; x1.5 for pixels) |
| `click PATH` | mouse-clicks a widget reached from the window, e.g. `click rail.buttons.words`, `click fetch_btn` |
| `ss NAME` | saves `shots/NAME.png` |
| `eval EXPR` / `exec STMT` | Python with `w` (the window), `app`, `archive`, `QTest`, `Qt`, `pump(ms)` |
| `requests` | what the fake archive was asked, by endpoint |
| `wait MS`, `quit` | |

Options: `--sample` (seed r/riverton, the README screenshots' data), `--db FILE` (open a copy of a database),
`--theme light`, `--size 1280x800`, `--screen 1920x1200 --scale 1.5`, `--real-speed`.

Fake subreddits: `python` (35 days, 6/12/0 posts a day), `rust` (3 a day, 10 days), `AskReddit` (150 a day for
3 days, "busy": big windows time out, as on the real archive). Anything else fetches nothing.

Interactive, under tmux:

```bash
tmux new-session -d -s ikari -x 200 -y 50 'python .claude/skills/run-ikari/driver.py --theme light 2>&1 | grep --line-buffered -v propagate'
tmux send-keys -t ikari 'fetch rust month nocomments' Enter
tmux send-keys -t ikari 'page posts' Enter 'ss rust-posts' Enter
tmux capture-pane -pt ikari
tmux send-keys -t ikari 'quit' Enter
```

Before/after screenshots for a GUI PR: run the same command script on `main` and on the branch, with `max` and
`restore`, in both themes. Look at every PNG (Read it) before posting.

## Direct invocation

Sentiment and other non-GUI code import straight from `src`:

```bash
PYTHONPATH=src python -c "from subsentiment import sentiment; print(sentiment.score('this update is great, thanks!'))"
```

The README screenshots (`docs/*.png`) come from `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python tools/screenshot.py --out DIR`.

## Test

```bash
python tools/check.py tests/test_sentiment.py -x   # while iterating; no args = the whole required check
```

## Run (human path)

`PYTHONPATH=src python -m subsentiment` opens the window with the real settings and data folder. Headless
(`QT_QPA_PLATFORM=offscreen`) it just sits there until killed: no way to see or drive it. Use the driver.

## Gotchas

- **Unstyled window.** A bare `MainWindow` renders as stock Qt (no cards, plain fonts) until `_apply_theme()`
  runs; the app's `main()` does that through `theme.apply`. The driver calls it; your own scripts must too.
- **Maximize gave a 531x531 window.** The offscreen platform's screen is 800x800 px by default. The driver
  passes `offscreen:configfile=screen.json` with logical DPI 144 over base 96 (how Windows' 150% reaches Qt), so
  `devicePixelRatio()` is 1.5 and maximized is 1916x1196 px. `QT_SCALE_FACTOR=1.5` (what `tools/screenshot.py`
  uses) scales right but keeps the tiny screen. Qt splits platform options at colons, so the config is passed by
  bare name from the driver's temp dir (the driver `chdir`s there; `--db`/`--out` are resolved first).
- **Restored size** is the app's own opening rule: 1203x736 logical (1805x1104 px) on this screen.
- **Modal boxes block forever headless.** The driver turns `QMessageBox.warning/information/critical` into a
  printed `[warning] title text` line, and answers `QMessageBox.question` **Yes**, so a delete you click goes through.
- **Pace.** Requests run 10x the app's pace (as `tests/test_app_e2e.py`). `--real-speed` uses the app's 0.4 s
  spacing: `fetch AskReddit day nocomments` took 12.4 s with it.
- **Clock** is pinned to noon UTC today (`fake_archive.Clock.pin`), so "Last fetched" always reads 12:00.
- `#` starts a comment, except on `eval`/`exec` lines. `w.posts` is a sequence: `len(w.posts)`, not `.count()`.
- `This plugin does not support propagateSizeHints()` on stderr is harmless offscreen noise.
- The cloud sandbox can't reach Arctic Shift. Changes to what's requested are checked live on the VPS
  (`tools/live_check.py`, `tools/live_gui_check.py`), not here.

## Troubleshooting

- `No module named ruff` from `tests/test_lint.py` (or `hypothesis` from `test_properties.py`): the image predates
  those dependencies; run `CLAUDE_CODE_REMOTE=true python tools/agent_setup.py`.
- `TypeError: Sequence.count() missing 1 required positional argument`: you called `.count()` on `w.posts`; use `len`.
