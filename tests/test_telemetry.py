"""OTel-aligned telemetry: off by default, JSONL spans when a sink is set, and the GenAI attribute
shape a model-call span carries.

The span-emitting call sites live in whatever component sits at the LLM chokepoint; this file tests
the module itself, which is provider-agnostic.
"""

import json

from agentrealm import telemetry

TOOLS = [{
    "type": "function",
    "function": {
        "name": "SendMessage",
        "description": "Post a message to a room.",
        "parameters": {"type": "object", "properties": {"body": {"type": "string"}}},
    },
}]


def test_disabled_by_default_is_a_noop(tmp_path, monkeypatch):
    for name in ("AGENTREALM_TELEMETRY", "AGENTREALM_LLM_TRACE"):
        monkeypatch.delenv(name, raising=False)
    assert telemetry.enabled() is False
    telemetry.emit_span("gen_ai.chat", {"x": 1})  # must not raise, must write nothing


def test_emit_span_writes_one_jsonl_line(tmp_path, monkeypatch):
    sink = tmp_path / "t.jsonl"
    monkeypatch.setenv("AGENTREALM_TELEMETRY", str(sink))
    telemetry.emit_span("gen_ai.chat", {"gen_ai.system": "azure"},
                        start_s=1000.0, duration_ms=12.34)
    line = sink.read_text().strip()
    rec = json.loads(line)
    assert rec["name"] == "gen_ai.chat"
    assert rec["attributes"]["gen_ai.system"] == "azure"
    assert rec["start_unix_nano"] == 1_000_000_000_000 and rec["duration_ms"] == 12.3


def test_the_alias_env_var_also_enables_the_sink(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTREALM_TELEMETRY", raising=False)
    sink = tmp_path / "alias.jsonl"
    monkeypatch.setenv("AGENTREALM_LLM_TRACE", str(sink))
    assert telemetry.enabled() is True
    telemetry.emit_span("gen_ai.chat", {"gen_ai.system": "azure"})
    assert json.loads(sink.read_text().strip())["name"] == "gen_ai.chat"


def test_llm_call_attributes_uses_otel_genai_conventions():
    attrs = telemetry.llm_call_attributes(
        system="azure", request_model="gpt-5.4", model_alias="realm-1--umpire",
        reasoning_effort="medium", system_prompt="You are Mother...", prompt="go",
        tool_schemas=TOOLS, completion="done", tool_calls=["rule"],
        input_tokens=100, output_tokens=20,
    )
    assert attrs["gen_ai.system"] == "azure"
    assert attrs["gen_ai.request.model"] == "gpt-5.4"
    assert attrs["gen_ai.usage.input_tokens"] == 100
    assert attrs["agentrealm.request.tool_names"] == ["SendMessage"]
    assert attrs["agentrealm.request.system_prompt"] == "You are Mother..."
    assert attrs["agentrealm.response.tool_calls"] == ["rule"]
