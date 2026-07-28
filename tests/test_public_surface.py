"""The repository must not carry private-fork vocabulary.

A separate private package provides a model pipeline that must never appear here — not in code,
tests, docs, comments, config, or a lockfile. This guard exists because "remember not to paste that
in" is not a control, and because the failure is silent and permanent once published.

Two layers, deliberately:

  * this test tolerates a small, explicit TRANSITIONAL list — entries that exist only while the
    plugin still lives in this workspace;
  * the seeding script (`scripts/seed-public-tree.sh`) tolerates NOTHING. What actually ships is
    checked against an empty allowance, so a transitional entry cannot survive into a release.

Add to TRANSITIONAL only with a tracking issue and a removal plan. Never widen a pattern to make a
failure go away — a failure here means something needs deleting, not excusing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Case-insensitive. Word-boundary anchored so "shim" does not match "shimmer" and "ali" does not
# match "alias" — a guard that cries wolf gets disabled, which is worse than no guard.
FORBIDDEN = (
    r"copilot",
    r"claude[-_ ]?cli",
    r"\bshim\b",
    r"\bshims\b",
    r"subscription",
    r"bearpit[-_]shim",
    # The owner's personal name as a bare word — it was the Herald operator default and is exactly
    # the kind of thing that gets re-added by accident. NOT `aliry`: that is the GitHub account this
    # repository lives under, so it appears legitimately in every project URL.
    r"\bali\b",
)

# (path-prefix, pattern-substring) pairs. An empty substring excuses every pattern for that path.
#
# Three tables with three different rationales, kept apart on purpose: conflating them is how a
# temporary exemption quietly becomes permanent.

# 1. Legitimate and permanent, in files that DO ship.
ALLOWED: tuple[tuple[str, str], ...] = (
    # These two name what they forbid.
    ("tests/test_public_surface.py", ""),
    ("scripts/seed-public-tree.sh", ""),
)

# 2. Paths excluded from the published tree entirely (see the seeding script). Their contents are
#    irrelevant to what ships, so the guard does not police them.
NOT_PUBLISHED: tuple[tuple[str, str], ...] = (
    ("packages/", ""),           # the private plugin, while it still shares this workspace
    ("spikes/", ""),             # throwaway spike code
    ("deploy/poc/", ""),         # the original proof-of-concept and its transcripts
    ("docs/internal/", ""),      # roadmap, prior-art research, specs, historical test reports
    (".claude/", ""),
)

# 3. Files that DO ship and carry plugin wiring only until the repo split. Each must be removed
#    then — `test_transitional_entries_are_still_needed` is the reminder, and it fires the moment
#    the excused term disappears.
#
#    Fenced because the seeding script removes this block wholesale: the published tree has no
#    plugin wiring, so it must not carry an exemption for it either. Keep the markers.
TRANSITIONAL: tuple[tuple[str, str], ...] = ()


# Directories that are never part of the published surface, whether or not git is present.
_SKIP_DIRS = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
              "node_modules", ".idea", ".vscode"}


def _tracked_files() -> list[str]:
    """Every file to police.

    Prefers `git ls-files`, which respects .gitignore exactly. Falls back to a filesystem walk so
    the guard also runs against a SEEDED tree — that directory is verified before `git init`, and a
    guard that silently cannot run there would be worse than none."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
        )
        return [line for line in out.stdout.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        found = []
        for path in REPO.rglob("*"):
            if not path.is_file():
                continue
            if _SKIP_DIRS & set(path.relative_to(REPO).parts):
                continue
            found.append(str(path.relative_to(REPO)))
        return sorted(found)


def _excused(path: str, pattern: str, table: tuple[tuple[str, str], ...]) -> bool:
    return any(
        path.startswith(allowed_path) and (not frag or frag in pattern)
        for allowed_path, frag in table
    )


def _violations(*, tables: tuple[tuple[tuple[str, str], ...], ...]) -> list[str]:
    found: list[str] = []
    compiled = [(p, re.compile(p, re.IGNORECASE)) for p in FORBIDDEN]
    for rel in _tracked_files():
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable — nothing textual to leak
        for raw, rx in compiled:
            if not rx.search(text):
                continue
            if any(_excused(rel, raw, table) for table in tables):
                continue
            line_no = next(
                (i for i, line in enumerate(text.splitlines(), 1) if rx.search(line)), 0
            )
            found.append(f"{rel}:{line_no} matches {raw!r}")
    return sorted(found)


def test_no_private_vocabulary_in_the_tracked_tree() -> None:
    """The whole repository, tolerating only what is documented above."""
    violations = _violations(tables=(ALLOWED, NOT_PUBLISHED, TRANSITIONAL))
    assert not violations, (
        "private-fork vocabulary found in tracked files:\n  "
        + "\n  ".join(violations)
        + "\n\nDelete it. Do not add it to ALLOWED unless it is genuinely a third-party reference."
    )


def test_the_guard_is_not_vacuous() -> None:
    """A guard that cannot fail protects nothing. This one has caught a real leak vector already:
    installing the plugin with `uv add` writes its distribution name into pyproject.toml and
    uv.lock, which is why both are on the TRANSITIONAL list rather than silently passing."""
    compiled = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN]
    for sample in ("we run copilot here", "the claude-cli path", "start the shim", "user ali"):
        assert any(rx.search(sample) for rx in compiled), sample
    # …and does not fire on ordinary words that merely contain a forbidden substring
    for benign in ("alias", "shimmering", "validate", "aliry/bearpit"):
        assert not any(rx.search(benign) for rx in compiled), benign


# The project was renamed from the former name to Bearpit. Assembled from fragments so this file
# does not itself contain the strings it forbids, which would need a self-exemption.
_FORMER = ("agent" + "realm", "a" + "realm")


def test_the_former_name_is_gone() -> None:
    """No stale references to the pre-Bearpit name survive anywhere in the tree.

    A rename is exactly the change a test suite is worst at policing. When this one was done, a
    single file was silently reverted mid-verification; `mypy` caught its two imports, but the same
    file also carried three user-facing `setup_hint` strings telling operators to run a command
    that no longer exists. Nothing in the suite would ever have failed on those — they are strings,
    not code. Hence a guard that reads the text."""
    rx = re.compile("|".join(rf"(?<![a-z]){re.escape(t)}(?![a-z])" for t in _FORMER), re.IGNORECASE)
    stale: list[str] = []
    for rel in _tracked_files():
        if rel == "tests/test_public_surface.py" or rel.startswith(("docs/adr/", "CHANGELOG")):
            continue  # this file names what it forbids; ADRs and changelogs record history
        try:
            text = (REPO / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                stale.append(f"{rel}:{i}: {line.strip()[:80]}")
    assert not stale, (
        "the former project name still appears in tracked files:\n  "
        + "\n  ".join(stale)
        + "\n\nRename them. Imports fail loudly; strings, docs and env-var names do not."
    )


def test_the_former_name_guard_is_not_vacuous() -> None:
    """It must catch the real regression that motivated it, and not fire on ordinary words."""
    rx = re.compile("|".join(rf"(?<![a-z]){re.escape(t)}(?![a-z])" for t in _FORMER), re.IGNORECASE)
    for sample in ("from " + "agentrealm" + ".core.plugins import x",
                   "run `" + "arealm" + " keys add openai-main`",
                   "AGENT" + "REALM_API_TOKEN"):
        assert rx.search(sample), sample
    for benign in ("bearpit.core.plugins", "BEARPIT_API_TOKEN", "the realm concluded", "realms"):
        assert not rx.search(benign), benign


@pytest.mark.parametrize("path,frag", TRANSITIONAL)
def test_transitional_entries_are_still_needed(path: str, frag: str) -> None:
    """Each exemption must still be EARNING its place.

    Asserting only that the file exists was too weak: `pyproject.toml` and `uv.lock` will outlive
    the plugin, so the reminder could never fire and the hole would have stayed open forever. This
    checks the excused term is actually still present — once the repo split strips the plugin
    wiring, this fails and the exemption has to go."""
    target = REPO / path
    if not target.exists():
        pytest.fail(f"{path} is gone — remove its TRANSITIONAL entry from this file")
    if target.is_file() and frag:
        assert frag in target.read_text(errors="ignore"), (
            f"{path} no longer contains {frag!r} — remove its TRANSITIONAL entry from this file"
        )
