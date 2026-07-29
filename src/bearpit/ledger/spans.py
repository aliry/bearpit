"""Turn LiteLLM spend-log rows into `gen_ai.chat` telemetry spans (#26).

The span contract (`bearpit/telemetry.py`) and its reader (`pit trace`) both existed, but nothing
captured raw LLM I/O for a realm running on an API provider — so `pit trace` came back empty for
every run on Azure, OpenAI, Anthropic or OpenRouter, and `docs/architecture.md` §16.5 described
a capability that was not there.

The capture point is LiteLLM's own `LiteLLM_SpendLogs` table, read through `/spend/logs`, which the
Ledger already polls for token counts. That is deliberate, and preferred over LiteLLM's logging
callbacks:

  * nothing is injected into a pinned third-party image, so the proxy stays exactly the pinned
    artifact and upgrades stay boring;
  * the poll loop already maps virtual key -> (realm, agent), which is the association a callback
    would have to rediscover;
  * a callback would need a host endpoint reachable from the container, plus its own auth — a new
    inbound surface next to the component that holds the real provider keys.

The cost is that rows are eventually-consistent (they flush a few seconds after the call) and that
LiteLLM must be told to keep the content: it redacts prompts and responses from spend logs unless
`STORE_PROMPTS_IN_SPEND_LOGS` is set. Both are documented in `docs/architecture.md` §16.5.

Everything here is pure and defensive: a row is third-party JSON that may be partial, redacted, or
shaped differently across proxy versions, and telemetry must never break the run it observes.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from bearpit.telemetry import llm_call_attributes


def _as_dict(value: Any) -> dict[str, Any]:
    """Rows arrive as parsed JSON, but some proxy versions hand back a JSON *string* per column."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _content_text(content: Any) -> str:
    """A message's text. Content is a plain string, or the multimodal list-of-parts form."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")]
        return "\n".join(parts)
    return ""


def _split_prompt(messages: list[Any]) -> tuple[str, str]:
    """(system prompt, everything else) — the split `pit trace` renders as `system:` and the rest.

    Kept identical to the shape the other capture path emits, so one reader renders both:
    system turns concatenated, then the remaining turns role-prefixed in order."""
    system: list[str] = []
    rest: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role", ""))
        text = _content_text(m.get("content"))
        if role == "system":
            if text:
                system.append(text)
        elif text:
            rest.append(f"{role}: {text}")
    return "\n\n".join(system), "\n\n".join(rest)


def _completion_and_tool_calls(response: dict[str, Any]) -> tuple[str, list[str]]:
    choices = _as_list(response.get("choices"))
    if not choices or not isinstance(choices[0], dict):
        return "", []
    message = _as_dict(choices[0].get("message"))
    completion = _content_text(message.get("content"))
    names = [
        str(_as_dict(tc.get("function")).get("name", ""))
        for tc in _as_list(message.get("tool_calls"))
        if isinstance(tc, dict)
    ]
    return completion, [n for n in names if n]


def _request_body(row: dict[str, Any]) -> dict[str, Any]:
    """The original request as the proxy received it — the only place the prompt actually lives.

    The `messages` column looks like the obvious source and is a trap: in the pinned proxy build
    `_get_messages_for_spend_logs_payload` returns content only when `call_type == "_arealtime"`,
    so for an ordinary chat completion it writes `{}` however the prompt-storage setting is
    configured. Confirmed against live rows: `messages` was 2 bytes (`{}`) while
    `proxy_server_request` was 7-92 KB and carried both the conversation and the tool schemas.

    Reading the column alone yields spans whose `system:` line is always blank — the one field the
    trace exists to show.

    Two shapes in the wild, and the obvious one is wrong. Inside LiteLLM the request is wrapped
    (`proxy_server_request["body"]`), but what it PERSISTS is the body itself, so a live row reads
    `{"model": …, "messages": [...], "tools": [...]}` with no wrapper. Following the source rather
    than the stored row cost a full validation cycle here: every span came back with no prompt and
    no tools. Unwrap when the wrapper exists, otherwise take the row as the body."""
    psr = _as_dict(row.get("proxy_server_request"))
    inner = _as_dict(psr.get("body"))
    return inner or psr


def _request_messages(row: dict[str, Any]) -> list[Any]:
    """Conversation turns: the request body first, the column as a fallback so a proxy version
    that does populate `messages` keeps working."""
    return _as_list(_request_body(row).get("messages")) or _as_list(row.get("messages"))


def _request_tools(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool schemas the model was OFFERED.

    Without these, `pit trace --tool X` cannot tell "never offered" from "offered and ignored",
    which is the distinction it exists to make."""
    return [t for t in _as_list(_request_body(row).get("tools")) if isinstance(t, dict)]


def _epoch_s(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:  # LiteLLM serialises ISO-8601, sometimes with a trailing Z
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _duration_ms(row: dict[str, Any], start_s: float | None) -> float | None:
    explicit = row.get("request_duration_ms")
    if isinstance(explicit, int | float):
        return float(explicit)
    end_s = _epoch_s(row.get("endTime"))
    if start_s is not None and end_s is not None and end_s >= start_s:
        return (end_s - start_s) * 1000.0
    return None


def span_from_spend_log(
    row: dict[str, Any], *, realm_id: str, agent_id: str
) -> tuple[dict[str, Any], float | None, float | None] | None:
    """One spend-log row -> (attributes, start_s, duration_ms), or None if it is not a chat call.

    Non-chat call types (embeddings, moderation) are skipped rather than emitted as empty chat
    spans, which would dilute `pit trace` with rows that have no prompt to show."""
    if not isinstance(row, dict):
        return None
    call_type = str(row.get("call_type") or "")
    if call_type and "completion" not in call_type and "chat" not in call_type:
        return None

    response = _as_dict(row.get("response"))
    completion, tool_calls = _completion_and_tool_calls(response)
    system_prompt, prompt = _split_prompt(_request_messages(row))
    start_s = _epoch_s(row.get("startTime"))
    status = str(row.get("status") or "")

    attrs = llm_call_attributes(
        # the upstream provider LiteLLM actually dispatched to — openai, azure, anthropic, …
        system=str(row.get("custom_llm_provider") or "litellm"),
        request_model=str(row.get("model") or "?"),
        realm_id=realm_id,
        agent_id=agent_id,
        # the public proxy model name, which is what the operator picked in Settings
        model_alias=str(row["model_group"]) if row.get("model_group") else None,
        system_prompt=system_prompt,
        prompt=prompt,
        tool_schemas=_request_tools(row),
        completion=completion,
        tool_calls=tool_calls,
        input_tokens=int(row["prompt_tokens"]) if row.get("prompt_tokens") is not None else None,
        output_tokens=(
            int(row["completion_tokens"]) if row.get("completion_tokens") is not None else None
        ),
        error=status if status and status != "success" else None,
    )
    return attrs, start_s, _duration_ms(row, start_s)


def request_id(row: dict[str, Any]) -> str | None:
    """The row's stable identity, so a poll loop never emits the same call twice."""
    rid = row.get("request_id") if isinstance(row, dict) else None
    return str(rid) if rid else None


__all__ = ["span_from_spend_log", "request_id"]
