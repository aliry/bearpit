"""The pitboss rubric is a 164-line numbered procedure, and a model follows the numbers.

Editing it is how it breaks: inserting a step and renumbering renumbered three OTHER sections too,
leaving the showdown procedure starting at "2." with no step 1 — and nothing would have failed.
These checks are cheap and catch exactly that: contiguous numbering inside each banner-delimited
section, and every "step N" cross-reference pointing at a step that exists.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

RUBRIC = (Path(__file__).resolve().parents[1]
          / "examples/poker-table/agents/pitboss/agent.json")
STEP = re.compile(r"^ (\d+)\. ")
BANNER = re.compile(r"^\s*={4,}")


@pytest.fixture
def sections() -> list[tuple[str, list[int]]]:
    """(section heading, the step numbers it declares) for each banner-delimited block."""
    lines = json.loads(RUBRIC.read_text())["rubric"].splitlines()
    out: list[tuple[str, list[int]]] = []
    head, nums = "(preamble)", []
    for ln in lines:
        if BANNER.match(ln):
            out.append((head, nums))
            head, nums = ln.strip("= ").strip(), []
        elif (m := STEP.match(ln)):
            nums.append(int(m.group(1)))
    out.append((head, nums))
    return [(h, n) for h, n in out if n]


def test_every_numbered_procedure_is_contiguous(sections):
    for head, nums in sections:
        assert nums == list(range(nums[0], nums[0] + len(nums))), (
            f"section {head!r} numbers its steps {nums} — a gap or repeat means the dealer is "
            f"told to do a step that is not there, or to skip one that is")


def test_only_the_wake_section_starts_at_zero(sections):
    """Step 0 is a deliberate signal — "before anything else". If another section acquires one it
    is almost certainly a renumbering accident like the one this file exists to catch."""
    zeros = [h for h, n in sections if n[0] == 0]
    assert zeros == ["WHEN THE MACHINE WAKES YOU MID-HAND"], zeros


def test_every_step_cross_reference_resolves(sections):
    """`step 2 of the wake section` has to name a step the wake section actually has."""
    wake = next(n for h, n in sections if h == "WHEN THE MACHINE WAKES YOU MID-HAND")
    text = json.loads(RUBRIC.read_text())["rubric"]
    refs = [int(m) for m in re.findall(r"step (\d+)(?:\(\w\))? of the wake section", text)]
    assert refs, "no cross-references found — has the wording changed?"
    for r in refs:
        assert r in wake, f"a cross-reference points at wake step {r}; the section has {wake}"


def test_the_dealer_is_told_to_read_the_floor_on_every_wake():
    """`table_talk` is useless if it is only mentioned in the preamble: the operating checklist is
    what gets followed. This pins it INSIDE the wake procedure."""
    lines = json.loads(RUBRIC.read_text())["rubric"].splitlines()
    start = next(i for i, ln in enumerate(lines)
                if "WHEN THE MACHINE WAKES YOU MID-HAND" in ln)
    end = next(i for i in range(start + 1, len(lines)) if BANNER.match(lines[i]))
    section = "\n".join(lines[start:end])
    assert "table_talk(" in section, "the wake procedure never tells the dealer to read the floor"
    assert "penalize(" in section, "the wake procedure reads the floor but never answers a card"
