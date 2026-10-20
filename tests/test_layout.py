"""Layout checks on every common screen and scaling: nothing cut off, squashed, overlapping or out of line.

Renders the real main window, each of its pages and drawers, offscreen with tools/ui_matrix.py (16:9, 16:10 and
MacBook sizes at their usual scalings; restored, maximized and dragged down to the minimum) and fails with
every problem it finds, one test and one render per screen, so the screens spread over the test workers.
To see what a failure looks like:

    python tools/ui_matrix.py --only win-1920x1200-150 --out shots/
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ui_matrix.py"
_spec = importlib.util.spec_from_file_location("ui_matrix", TOOL)
ui_matrix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui_matrix)
ROWS = ui_matrix.screens()


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    """The sample database each screen renders, seeded once per test worker."""
    sys.path.insert(0, str(TOOL.parent))
    import screenshot
    tmp = tmp_path_factory.mktemp("ui-matrix")
    screenshot.seed(str(tmp / "sample.db"))
    return tmp


@pytest.mark.parametrize("row", ROWS, ids=[row[0] for row in ROWS])
def test_layout(sample, row):
    # The matrix's own options, screenshots off.
    args = types.SimpleNamespace(theme="dark", no_sweep=False, subpixel=False, no_shots=True)
    report = ui_matrix.run_one(row, sample / "sample.db", sample, sample, args)
    assert not isinstance(report, str), report
    problems = [f"{r['state']} at {r['size'][0]}x{r['size'][1]}: {p}" for r in report["results"] for p in r["problems"]]
    assert not problems, f"{len(problems)} layout problems on {row[0]}:\n" + "\n".join(problems)
