# ikari: rules for coding agents

ikari is a PySide6 desktop app (package `subsentiment`) that fetches subreddit posts from the Arctic Shift
archive and scores sentiment offline. The owner runs current `main` from source on Windows, 1920x1200 at 150%.

## Checks
- `python tools/check.py` runs exactly what the required PR check runs. While iterating, pass it the test
  files you're touching (`python tools/check.py tests/test_store.py -x`).
- The session-start hook turns on `.githooks/pre-push`, so a push only goes through when the branch is up to
  date with `origin/main` and the whole suite passes. Merge `origin/main` in when it says so; don't use
  `--no-verify`.
- Never delete, skip or loosen an existing test to get green. If a test is wrong, say so in the PR.
- After three failed attempts at the same failure, stop and report what fails and why.
- If `main` itself is red, the pre-push check blocks every branch. The thread whose PR broke it fixes `main`
  first; everyone else waits for that fix rather than pushing around the check.

## Doing the work
- Reproduce a reported bug on current `main` first. If it happens there, it's a `main` bug, never the
  owner's setup.
- Logic bug fixes come with a test that fails on `main` and passes on the branch. Visual fixes come with
  before/after screenshots instead.
- Before opening a PR, check open PRs for ones touching the same files; build on them rather than overlap.
- Behaviour changes are exercised the way a user would: offscreen Qt driving the real `MainWindow` against
  `tests/fake_archive.py`, with a test added. Nothing may fetch or download unless the user asked.
- Changes to what is requested from Arctic Shift are also checked live (`tools/live_check.py`,
  `tools/live_gui_check.py`) from a machine that can reach it; the cloud sandbox can't.
- GUI changes: before/after screenshots at 1920x1200 150%, maximized and restored, with text checked at a real
  1.5 device pixel ratio. The layout tests cover the other resolutions.

## PRs
- The PR says what was checked, with output or screenshots, and what wasn't. A claim with no output is
  unverified.
- For non-trivial changes, review the diff in a fresh context (`/code-review`) and fix correctness findings.
- The thread that opens a PR keeps it mergeable: when `main` moves, merge it in and push.
- CI and workflow changes go in their own PR and name every required check they add, rename or remove.

## Repo facts
- Commit author: `nekomancer01 <5332082+nekomancer01@users.noreply.github.com>`.
- Never rename storage identifiers (data folder, QSettings names); users' data depends on them.
- Windows-only CI; Mac/Linux jobs stay behind the manual `all_platforms` input with their code intact.
- Releases: bump `__version__` in a PR, then tag `v<version>` on `main`.
