"""Scribe — the built-in conversational scenario-authoring assistant (host control-plane).

Scribe owns a tool-use loop (§6) behind a narrow, provider-swappable `LLMBackend` contract (§7):
describe a scenario in natural language and it creates/edits/validates scenario packages, with a
curatable memory and version history. It is a trusted control-plane actor, not a realm agent.
"""

from __future__ import annotations

from agentrealm.scribe.backend import LLMBackend, OpenAIBackend
from agentrealm.scribe.loop import LoopEvent, ScribeLoop
from agentrealm.scribe.memory import Memory
from agentrealm.scribe.service import ScribeSession, build_scribe
from agentrealm.scribe.store import ApiPackageStore, PackageStore
from agentrealm.scribe.tools import TOOL_SPECS, AuthoringTools
from agentrealm.scribe.types import Completion, Message, ToolCall, ToolSpec, Usage
from agentrealm.scribe.validate import ValidationResult, validate_scenario
from agentrealm.scribe.versions import Versions, diff_projects

__all__ = [
    "ApiPackageStore",
    "AuthoringTools",
    "Completion",
    "LLMBackend",
    "LoopEvent",
    "Memory",
    "Message",
    "OpenAIBackend",
    "PackageStore",
    "ScribeLoop",
    "ScribeSession",
    "TOOL_SPECS",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "ValidationResult",
    "Versions",
    "build_scribe",
    "diff_projects",
    "validate_scenario",
]
