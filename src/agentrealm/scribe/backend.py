"""The `LLMBackend` contract (§7) and `OpenAIBackend`, its implementation.

The whole seam between Scribe and any model provider is one method, `complete()`. `OpenAIBackend`
implements it against any OpenAI-compatible `/chat/completions` endpoint — Azure OpenAI, OpenAI,
Anthropic through a proxy, OpenRouter, a LiteLLM proxy, or a local server. Point it at a base URL,
give it a key, name a model.

Scribe owns its own tool-use loop, so this layer stays deliberately thin: it maps messages and tool
specs onto the wire format, and maps `tool_calls` and usage back. Whether the endpoint implements
function calling natively or emulates it makes no difference here — both return the same shape.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from agentrealm.scribe.types import Completion, Message, ToolCall, ToolSpec, Usage

# Overridable per call, per `build_scribe`, or with SCRIBE_MODEL. Whatever it is, it must be a model
# the configured endpoint actually serves.
DEFAULT_MODEL = "claude-sonnet-5"


class LLMBackend(Protocol):
    """The entire seam between Scribe and any model provider — one method."""

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        effort: str | None = None,
    ) -> Completion: ...


def _message_to_openai(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in m.tool_calls
        ]
    if m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    return out


def _tool_to_openai(t: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
    }


def _parse_completion(data: dict[str, Any]) -> Completion:
    choices = data.get("choices") or [{}]
    msg = choices[0].get("message") or {}
    text = msg.get("content")
    calls: list[ToolCall] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append(
            ToolCall(
                id=str(tc.get("id") or f"call_{i}"),
                name=str(fn.get("name") or ""),
                arguments=args,
            )
        )
    u = data.get("usage") or {}
    usage = Usage(
        tokens_in=int(u.get("prompt_tokens") or 0),
        tokens_out=int(u.get("completion_tokens") or 0),
    )
    return Completion(text=text, tool_calls=calls, usage=usage)


class OpenAIBackend:
    """An OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._owns_client = client is None
        # One authoring turn is several model calls; keep the per-request timeout generous.
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        effort: str | None = None,
    ) -> Completion:
        body: dict[str, Any] = {
            "model": model or self._model,
            "messages": [_message_to_openai(m) for m in messages],
            "tools": [_tool_to_openai(t) for t in tools],
            "stream": False,
        }
        if effort:
            body["reasoning_effort"] = effort
        resp = await self._client.post(
            f"{self._base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        resp.raise_for_status()
        return _parse_completion(resp.json())

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this backend created it."""
        if self._owns_client:
            await self._client.aclose()
