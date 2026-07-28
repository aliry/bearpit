"""Lightweight, OpenTelemetry-aligned telemetry — emitted as JSONL spans.

Purpose: capture structured, machine-readable spans (LLM calls today; other operations later) for
debugging and observability. Field names follow OpenTelemetry **semantic conventions** — the GenAI
conventions (`gen_ai.*`) for model calls — so the SAME call sites can later be exported through the
OTel SDK (OTLP → collector → Jaeger/Tempo/Grafana) instead of a flat file, with no change to the
callers. See `docs/architecture.md` §16.5 for the span shape and the migration path.

Zero dependencies and off by default: every call is a cheap no-op unless a sink path is set in the
environment. Enable by pointing any of these at a writable file:

    BEARPIT_TELEMETRY=/path/to/telemetry.jsonl   # preferred
    BEARPIT_LLM_TRACE=...                         # alias

Each line is one span object: {name, start_unix_nano, duration_ms, attributes:{...}}. Telemetry is
best-effort — a failure to record NEVER propagates to the caller's request path.
"""

from __future__ import annotations

import json
import os
from typing import Any

# checked in order; the first set wins. Two names so existing setups keep working.
_SINK_ENV = ("BEARPIT_TELEMETRY", "BEARPIT_LLM_TRACE")


def sink_path() -> str | None:
    """The configured telemetry sink file, or None when telemetry is disabled."""
    for name in _SINK_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return None


def enabled() -> bool:
    return sink_path() is not None


def emit_span(
    name: str,
    attributes: dict[str, Any],
    *,
    start_s: float | None = None,
    duration_ms: float | None = None,
) -> None:
    """Append one span as a JSON line to the configured sink. No-op (and never raises) when
    telemetry is off — telemetry must not be able to break the operation it observes."""
    path = sink_path()
    if not path:
        return
    try:
        record: dict[str, Any] = {"name": name, "attributes": attributes}
        if start_s is not None:
            record["start_unix_nano"] = int(start_s * 1_000_000_000)
        if duration_ms is not None:
            record["duration_ms"] = round(duration_ms, 1)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - telemetry is best-effort; swallow everything
        pass


def llm_call_attributes(
    *,
    system: str,
    request_model: str,
    realm_id: str | None = None,
    agent_id: str | None = None,
    model_alias: str | None = None,
    reasoning_effort: str | None = None,
    system_prompt: str = "",
    prompt: str = "",
    tool_schemas: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    completion: str = "",
    tool_calls: list[str] | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    argv: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the attribute map for one LLM chat call, using OTel GenAI semantic conventions where
    they exist (`gen_ai.*`) plus `bearpit.*` for what OTel doesn't cover yet (the full rendered
    system prompt, tool JSON schemas, the exact `claude` argv). The full system prompt + completion
    are the fields that answer "what did this agent's model actually receive and produce?"."""
    tools = tool_schemas or []
    attrs: dict[str, Any] = {
        "gen_ai.system": system,
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": request_model,
        # the full rendered inputs — the point of the trace (OTel has no stable content attr yet)
        "bearpit.request.system_prompt": system_prompt,
        "bearpit.request.prompt": prompt,
        "bearpit.request.tool_names": [
            t.get("function", {}).get("name") for t in tools if isinstance(t, dict)
        ],
        "bearpit.request.tool_schemas": tools,
        "bearpit.request.tool_choice": tool_choice,
        # the full raw output + any tool calls the model actually emitted
        "bearpit.response.completion": completion,
        "bearpit.response.tool_calls": tool_calls or [],
    }
    if realm_id is not None:
        attrs["bearpit.realm.id"] = realm_id  # first-class filter: one realm's calls
    if agent_id is not None:
        attrs["bearpit.agent.id"] = agent_id  # and one agent's calls within it
    if model_alias is not None:
        attrs["bearpit.request.model_alias"] = model_alias  # as the provider encoded it
    if reasoning_effort is not None:
        attrs["gen_ai.request.reasoning_effort"] = reasoning_effort
    if input_tokens is not None:
        attrs["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        attrs["gen_ai.usage.output_tokens"] = output_tokens
    if argv is not None:
        attrs["bearpit.request.argv"] = argv
    if error is not None:
        attrs["error"] = True
        attrs["bearpit.error.message"] = error
    return attrs


__all__ = ["sink_path", "enabled", "emit_span", "llm_call_attributes"]
