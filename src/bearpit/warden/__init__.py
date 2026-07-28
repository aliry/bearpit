"""Warden: lifecycle + event engine.

Watchers, termination rules, concluding sequence, kill switch (M5, §11).
"""

from bearpit.warden.termination import (
    RealmSnapshot,
    TerminationFired,
    evaluate_termination,
)
from bearpit.warden.turns import TurnBus, TurnManager
from bearpit.warden.warden import ConcludeResult, Warden

__all__ = [
    "ConcludeResult",
    "RealmSnapshot",
    "TerminationFired",
    "TurnBus",
    "TurnManager",
    "Warden",
    "evaluate_termination",
]
