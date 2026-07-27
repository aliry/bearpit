"""The narrow value types that make up Scribe's LLM-backend contract (§7).

These are provider-agnostic on purpose: `OpenAIBackend` and any future backend
(AOAI/Anthropic) both speak in exactly these dataclasses, so swapping the model is a config
change, not a rewrite. Frozen dataclasses keep them cheap to pass around and hashable-by-field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """A structured tool invocation the model asked for (decoded arguments, not a JSON blob)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    """One turn in the conversation. `role` is system/user/assistant/tool.

    An assistant message may carry `tool_calls`; a `role="tool"` message carries the result of one
    call, tagged with the `tool_call_id` it answers.
    """

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model: a name, a description, and a JSON-Schema for its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """Token accounting for one completion (0 when the backend does not report it)."""

    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(frozen=True)
class Completion:
    """What a backend returns: assistant `text` and/or `tool_calls`, plus `usage`."""

    text: str | None
    tool_calls: list[ToolCall]
    usage: Usage
