"""Per-agent display colors: auto-assignment (core/colors.py), schema validation, and the
serialized package view the UI reads."""

import pytest
from pydantic import ValidationError

from bearpit.core import load_package
from bearpit.core.colors import AGENT_COLOR_PALETTE, resolve_agent_colors
from bearpit.core.schema import AgentSpec
from bearpit.gatekeeper.api import serialize_project


class _A:
    def __init__(self, id: str, color: str | None = None) -> None:
        self.id = id
        self.color = color


def test_auto_assigns_distinct_colors_by_roster_order():
    colors = resolve_agent_colors([_A("a"), _A("b"), _A("c")])
    assert colors == {
        "a": AGENT_COLOR_PALETTE[0], "b": AGENT_COLOR_PALETTE[1], "c": AGENT_COLOR_PALETTE[2],
    }
    assert len(set(colors.values())) == 3


def test_explicit_color_wins_and_auto_skips_it():
    # 'b' pins palette[0]; the auto-assigned agents must skip it so every color stays unique
    colors = resolve_agent_colors([_A("a"), _A("b", AGENT_COLOR_PALETTE[0]), _A("c")])
    assert colors["b"] == AGENT_COLOR_PALETTE[0]
    assert colors["a"] != AGENT_COLOR_PALETTE[0] and colors["c"] != AGENT_COLOR_PALETTE[0]
    assert len(set(colors.values())) == 3


def test_deterministic():
    assert resolve_agent_colors([_A("x"), _A("y"), _A("z")]) == \
        resolve_agent_colors([_A("x"), _A("y"), _A("z")])


def test_more_agents_than_palette_wraps_without_crashing():
    n = len(AGENT_COLOR_PALETTE) + 3
    colors = resolve_agent_colors([_A(f"a{i}") for i in range(n)])
    assert len(colors) == n  # everyone gets a color


def test_agentspec_color_validates_hex():
    assert AgentSpec(id="a", color="#1A2b3C").color == "#1A2b3C"
    assert AgentSpec(id="a").color is None
    for bad in ("red", "#12345", "1a2b3c", "#12345g"):
        with pytest.raises(ValidationError):
            AgentSpec(id="a", color=bad)


def test_serialized_package_exposes_distinct_agent_colors():
    proj = load_package("examples/cygnus-crew")
    view = serialize_project(proj, "cygnus-crew", "examples/cygnus-crew")
    colors = [a["color"] for a in view["agents"]]
    assert colors and all(isinstance(c, str) and c.startswith("#") for c in colors)
    assert len(set(colors)) == len(colors)  # each agent visually distinct
