#!/bin/sh
# Run ikari from source on macOS or Linux.
# First run creates a private Python environment in .venv (takes a minute).
# Delete the .venv folder to reinstall from scratch.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    PY="$(command -v python3 || true)"
    if [ -z "$PY" ]; then
        echo "Python 3.10 or newer is needed: https://www.python.org/downloads/" >&2
        exit 1
    fi
    echo "First run: setting up ikari..."
    if ! "$PY" -m venv .venv; then
        rm -rf .venv
        echo "Could not create a virtual environment. On Debian/Ubuntu: sudo apt install python3-venv" >&2
        exit 1
    fi
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e .
fi
exec .venv/bin/python -m subsentiment "$@"
