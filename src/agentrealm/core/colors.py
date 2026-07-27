"""Per-agent display colors for the UI.

Every agent gets a distinct color that its messages render in on the realm page. A color is
auto-assigned by roster order so a scenario needs no manual setup, but an explicit
`AgentSpec.color` (set in the scenario editor) always wins. Kept here — a tiny, pure helper —
so the API, and any other consumer, resolve colors the same way.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class _Colorable(Protocol):
    id: str
    color: str | None

# Distinct mid-tone hues chosen to stay legible on both light and dark feed backgrounds. Order is
# the assignment order; the JS feed mirrors this list so auto-assigned colors match before a run's
# scenario has been reloaded server-side.
AGENT_COLOR_PALETTE: tuple[str, ...] = (
    "#e15759", "#4e9bd9", "#59a14f", "#eda13a", "#b07aa1", "#4bc0c0",
    "#e377c2", "#9c755f", "#7fbf4f", "#6a8ec9", "#d4a017", "#ba5fd0",
    "#52b3a4", "#c9739b",
)


def resolve_agent_colors(agents: Iterable[_Colorable]) -> dict[str, str]:
    """Map each agent's id to a hex color, in the given (display) order.

    An agent's explicit `color` is honored and reserved first; the rest are filled from the
    palette, skipping colors already taken, so the assignment is deterministic and collision-free
    up to the palette size (beyond it, colors wrap — still deterministic). Each item needs an `id`
    and a `color` attribute (an AgentSpec, or any object exposing them)."""
    ordered = list(agents)
    used: set[str] = {a.color.lower() for a in ordered if a.color}
    result: dict[str, str] = {}
    palette_i = 0
    for a in ordered:
        if a.color:
            result[a.id] = a.color
            continue
        while (palette_i < len(AGENT_COLOR_PALETTE)
               and AGENT_COLOR_PALETTE[palette_i].lower() in used):
            palette_i += 1
        if palette_i < len(AGENT_COLOR_PALETTE):
            color = AGENT_COLOR_PALETTE[palette_i]
            palette_i += 1
        else:  # roster larger than the palette — wrap deterministically
            color = AGENT_COLOR_PALETTE[len(result) % len(AGENT_COLOR_PALETTE)]
        used.add(color.lower())
        result[a.id] = color
    return result
