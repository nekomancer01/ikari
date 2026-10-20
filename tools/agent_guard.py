"""Blocks `git push --no-verify` for coding agents, run by the PreToolUse hook in .claude/settings.json.

Skipping the pre-push hook skips tools/check.py, the check every push has to pass. Reads the hook's JSON on stdin;
exit code 2 refuses the command and shows the message to the agent.
"""
from __future__ import annotations

import json
import re
import sys


def main() -> int:
    try:
        command = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    except (ValueError, AttributeError):
        return 0
    # Only a git command at the start of a shell segment, so prose mentioning it (a commit message) passes.
    if re.search(r"(?:^|[;&|(]|\n)\s*git\b[^;&|\n]*\spush\b[^;&|\n]*\s--no-verify\b", command):
        print("Pushing with --no-verify skips tools/check.py. Fix what it reports, or say what fails and why.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
