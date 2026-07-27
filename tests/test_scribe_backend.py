"""OpenAIBackend maps Scribe's LLM contract to/from an OpenAI-compatible chat-completions API.

The layer is deliberately thin — Scribe owns its own tool-use loop — so what matters here is that
the wire mapping is faithful in both directions: tool specs out, `tool_calls` and usage back,
whether the endpoint does function calling natively or emulates it.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
from fakes import FakeLLMBackend

from agentrealm.scribe.backend import OpenAIBackend
from agentrealm.scribe.types import Message, ToolCall, ToolSpec


def _spec() -> ToolSpec:
    return ToolSpec(
        name="validate_scenario",
        description="check a scenario",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )


async def test_complete_posts_openai_body_and_parses_tool_calls() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_0",
                                    "type": "function",
                                    "function": {
                                        "name": "validate_scenario",
                                        "arguments": json.dumps({"name": "duel"}),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAIBackend(
        "http://models.test/v1", api_key="k-123", model="claude-sonnet-5", client=client
    )
    comp = await backend.complete(
        [Message(role="user", content="make a duel")], [_spec()], model="claude-sonnet-5"
    )

    assert captured["url"] == "http://models.test/v1/chat/completions"
    assert captured["auth"] == "Bearer k-123"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["tools"][0] == {
        "type": "function",
        "function": {
            "name": "validate_scenario",
            "description": "check a scenario",
            "parameters": _spec().parameters,
        },
    }
    assert body["messages"][0] == {"role": "user", "content": "make a duel"}

    assert comp.text is None
    assert len(comp.tool_calls) == 1
    assert comp.tool_calls[0].name == "validate_scenario"
    assert comp.tool_calls[0].arguments == {"name": "duel"}
    assert comp.usage.tokens_in == 11
    assert comp.usage.tokens_out == 3
    await client.aclose()


async def test_complete_tolerates_dict_arguments_and_maps_tool_history() -> None:
    """`arguments` may arrive as a dict (not a JSON string); assistant/tool history round-trips."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "done",
                            "tool_calls": [
                                {
                                    "id": "x",
                                    "type": "function",
                                    "function": {"name": "read_scenario", "arguments": {"n": 1}},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = OpenAIBackend("http://models.test/v1", client=client)
    history = [
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="a1", name="list_scenarios", arguments={})],
        ),
        Message(role="tool", content="[]", tool_call_id="a1"),
    ]
    comp = await backend.complete(history, [_spec()], model="claude-sonnet-5", effort="high")

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["reasoning_effort"] == "high"
    assert body["messages"][0]["tool_calls"][0]["function"]["name"] == "list_scenarios"
    assert body["messages"][0]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert body["messages"][1] == {"role": "tool", "content": "[]", "tool_call_id": "a1"}
    assert comp.tool_calls[0].arguments == {"n": 1}
    assert comp.text == "done"
    await client.aclose()


async def test_fake_backend_scripts_completions() -> None:
    from agentrealm.scribe.types import Completion, Usage

    fake = FakeLLMBackend([Completion(text="hi", tool_calls=[], usage=Usage())])
    comp = await fake.complete([Message(role="user", content="yo")], [], model="m")
    assert comp.text == "hi"
    assert len(fake.calls) == 1


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("SCRIBE_LIVE"),
    reason="hits a real endpoint; set SCRIBE_LIVE=1 to run the §15 tool-reliability gate",
)
async def test_tool_call_reliability() -> None:
    """§15 risk gate: tool calling must be reliable on whatever endpoint Scribe is pointed at.

    Send 10 tool-heavy prompts with a 2-tool spec; require >= 9/10 to produce a well-formed
    tool-call. Record the pass rate in the PR. If it fails badly, STOP and escalate — do not build
    on a flaky backend.
    """
    base_url = os.environ.get("SCRIBE_API_BASE", "http://127.0.0.1:4000/v1")
    api_key = os.environ.get("SCRIBE_API_KEY", "")
    model = os.environ.get("SCRIBE_MODEL", "claude-sonnet-5")
    tools = [
        ToolSpec(
            name="create_scenario",
            description="Create a scenario package from a spec.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["name"],
            },
        ),
        ToolSpec(
            name="list_scenarios",
            description="List existing scenarios.",
            parameters={"type": "object", "properties": {}},
        ),
    ]
    backend = OpenAIBackend(base_url, api_key=api_key, model=model)
    ok = 0
    for i in range(10):
        comp = await backend.complete(
            [
                Message(
                    role="user",
                    content=f"Create a scenario named duel-{i}. Call create_scenario now.",
                )
            ],
            tools,
            model=model,
        )
        if any(c.name == "create_scenario" for c in comp.tool_calls):
            ok += 1
    await backend.aclose()
    assert ok >= 9, f"emulated function-calling only {ok}/10 — escalate before building on it"
