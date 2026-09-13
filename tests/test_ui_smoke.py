"""Run the console's JavaScript smoke harness from the Python suite.

`tests/ui_smoke.mjs` evaluates the real `app.js` in a small DOM shim and drives its real render
functions. It lives outside pytest because it is JavaScript; it is run from pytest because a
check nobody runs is not a check. See the harness header for what each check covers.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "ui_smoke.mjs"
REPO_ROOT = Path(__file__).resolve().parents[1]

node = shutil.which("node")
if node is None:
    pytest.skip(
        "node is not installed — the console's JS is unexercised here", allow_module_level=True
    )

RESULT = subprocess.run(  # a fixed argv, no shell
    [node, str(HARNESS)],
    capture_output=True,
    text=True,
    timeout=60,
    cwd=REPO_ROOT,
    check=False,
)

# One "CHECK <name> PASS|FAIL <detail>" line per check, so a failure names itself in the pytest
# report instead of hiding inside one opaque assertion.
CHECKS = [
    tuple(line.split(" ", 3)[1:])
    for line in RESULT.stdout.splitlines()
    if line.startswith("CHECK ")
]


def _report() -> str:
    """The harness's whole output — this is the diagnostic when CI goes red."""
    return (
        f"\n$ node {HARNESS} (exit {RESULT.returncode})"
        f"\n--- stdout ---\n{RESULT.stdout}"
        f"\n--- stderr ---\n{RESULT.stderr}"
    )


@pytest.mark.parametrize(
    ("name", "status", "detail"), CHECKS, ids=[c[0] for c in CHECKS] or None
)
def test_console_js(name: str, status: str, detail: str) -> None:
    assert status == "PASS", f"console check {name!r} failed: {detail}{_report()}"


def test_harness_itself_ran() -> None:
    """A harness that crashes before its first check would leave the parametrised test with an
    empty parameter set — which pytest reports as a skip, not a failure. Catch that here."""
    assert CHECKS, f"the harness reported no checks at all{_report()}"
    summary = [ln for ln in RESULT.stdout.splitlines() if ln.startswith("SUMMARY ")]
    assert summary, f"the harness did not finish (no SUMMARY line){_report()}"
    assert int(summary[0].split()[1]) == len(CHECKS), f"checks went missing{_report()}"
    assert RESULT.returncode == 0, f"the harness exited non-zero{_report()}"
