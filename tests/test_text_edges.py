"""Windows text has no ClearType colour fringes at a real 150% scaling (tools/text_aa_check.py).

The layout matrix renders offscreen, which never shows how Windows draws glyphs, so this one runs on the
real "windows" platform. Skipped elsewhere."""
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "text_aa_check.py"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows font rendering only")
def test_windows_text_has_no_colour_fringes():
    proc = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
