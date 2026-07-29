"""LiteLLM spend-log rows -> `gen_ai.chat` spans (#26).

The rows are third-party JSON: partial, redacted, or shaped differently across proxy versions. The
converter is pure so every one of those shapes is testable without a proxy, and the assertions are
written against the attributes `pit trace` actually renders — a span that parses but renders blank
would be worse than no span at all.
"""

from bearpit.ledger.spans import request_id, span_from_spend_log

ROW = {
    "request_id": "req-1",
    "call_type": "acompletion",
    "model": "gpt-5.4",
    "model_group": "medium",
    "custom_llm_provider": "openai",
    "prompt_tokens": 1200,
    "completion_tokens": 64,
    "startTime": "2026-07-28T04:50:00",
    "endTime": "2026-07-28T04:50:02.500000",
    "status": "success",
    "messages": [
        {"role": "system", "content": "You are orin. Win the duel."},
        {"role": "user", "content": "Round R1 — post your move."},
    ],
    "response": {
        "choices": [{
            "message": {
                "content": "Sealing my move.",
                "tool_calls": [{"function": {"name": "submit_sealed"}}],
            },
        }],
    },
    "proxy_server_request": {
        "body": {"tools": [
            {"type": "function", "function": {"name": "submit_sealed"}},
            {"type": "function", "function": {"name": "recall"}},
        ]},
    },
}


def test_a_row_renders_everything_pit_trace_shows() -> None:
    built = span_from_spend_log(ROW, realm_id="duel", agent_id="orin")
    assert built is not None
    attrs, start_s, duration_ms = built

    # the filters
    assert attrs["bearpit.realm.id"] == "duel"
    assert attrs["bearpit.agent.id"] == "orin"
    # the header line: model, tokens, duration
    assert attrs["bearpit.request.model_alias"] == "medium"
    assert attrs["gen_ai.request.model"] == "gpt-5.4"
    assert attrs["gen_ai.system"] == "openai"
    assert (attrs["gen_ai.usage.input_tokens"], attrs["gen_ai.usage.output_tokens"]) == (1200, 64)
    assert duration_ms == 2500.0 and start_s is not None
    # the body: system prompt split from the rest, completion, and BOTH tool lists
    assert attrs["bearpit.request.system_prompt"] == "You are orin. Win the duel."
    assert attrs["bearpit.request.prompt"] == "user: Round R1 — post your move."
    assert attrs["bearpit.response.completion"] == "Sealing my move."
    assert attrs["bearpit.request.tool_names"] == ["submit_sealed", "recall"]
    assert attrs["bearpit.response.tool_calls"] == ["submit_sealed"]
    assert "error" not in attrs


def test_offered_and_called_are_distinguishable() -> None:
    """`pit trace --tool X` exists to separate "never offered" from "offered and ignored". That
    needs the request's tool schemas, which live only in `proxy_server_request` — reading tools
    from `messages` would silently collapse the two cases into one."""
    built = span_from_spend_log(ROW, realm_id="duel", agent_id="orin")
    assert built is not None
    attrs = built[0]
    offered, called = attrs["bearpit.request.tool_names"], attrs["bearpit.response.tool_calls"]
    assert "recall" in offered and "recall" not in called      # offered, ignored
    assert "submit_sealed" in offered and "submit_sealed" in called
    assert "eliminate" not in offered                           # never offered


def test_a_redacted_row_still_produces_a_usable_span() -> None:
    """Content is empty unless the proxy runs with STORE_PROMPTS_IN_SPEND_LOGS. The span must still
    carry model, tokens and timing rather than being dropped — that is the signal the operator
    needs to notice the setting is off."""
    row = {**ROW, "messages": {}, "response": {}, "proxy_server_request": {}}
    built = span_from_spend_log(row, realm_id="duel", agent_id="orin")
    assert built is not None
    attrs = built[0]
    assert attrs["gen_ai.usage.input_tokens"] == 1200
    assert attrs["bearpit.request.system_prompt"] == ""
    assert attrs["bearpit.response.completion"] == ""
    assert attrs["bearpit.request.tool_names"] == []


def test_json_encoded_columns_are_parsed() -> None:
    """Some proxy versions hand back each JSON column as a *string* rather than parsed."""
    import json
    row = {**ROW,
           "messages": json.dumps(ROW["messages"]),
           "response": json.dumps(ROW["response"]),
           "proxy_server_request": json.dumps(ROW["proxy_server_request"])}
    built = span_from_spend_log(row, realm_id="duel", agent_id="orin")
    assert built is not None
    attrs = built[0]
    assert attrs["bearpit.request.system_prompt"] == "You are orin. Win the duel."
    assert attrs["bearpit.response.tool_calls"] == ["submit_sealed"]
    assert attrs["bearpit.request.tool_names"] == ["submit_sealed", "recall"]


def test_multimodal_content_parts_are_flattened() -> None:
    row = {**ROW, "messages": [
        {"role": "system", "content": [{"type": "text", "text": "be brief"}]},
        {"role": "user", "content": [{"type": "text", "text": "go"},
                                     {"type": "image_url", "image_url": {"url": "…"}}]},
    ]}
    built = span_from_spend_log(row, realm_id="d", agent_id="o")
    assert built is not None
    assert built[0]["bearpit.request.system_prompt"] == "be brief"
    assert built[0]["bearpit.request.prompt"] == "user: go"


def test_a_failed_call_is_marked_as_an_error() -> None:
    built = span_from_spend_log({**ROW, "status": "failure"}, realm_id="d", agent_id="o")
    assert built is not None
    assert built[0]["error"] is True
    assert built[0]["bearpit.error.message"] == "failure"


def test_non_chat_rows_are_skipped() -> None:
    """An embeddings row has no prompt to show; emitting it would only dilute the trace."""
    row = {**ROW, "call_type": "aembedding"}
    assert span_from_spend_log(row, realm_id="d", agent_id="o") is None


def test_a_row_with_nothing_in_it_does_not_explode() -> None:
    built = span_from_spend_log({}, realm_id="d", agent_id="o")
    assert built is not None                       # no call_type => assume chat
    assert built[0]["gen_ai.request.model"] == "?"
    assert built[1] is None and built[2] is None   # unknown timings, not fabricated ones


def test_request_id_is_the_dedupe_key() -> None:
    assert request_id(ROW) == "req-1"
    assert request_id({}) is None                  # no id => cannot dedupe => never emitted twice


def test_the_prompt_comes_from_the_request_body_not_the_messages_column() -> None:
    """The `messages` column is a trap, and only a live run reveals it.

    In the pinned proxy build `_get_messages_for_spend_logs_payload` returns content only for
    `call_type == "_arealtime"`; an ordinary chat completion writes `{}` no matter how prompt
    storage is configured. Live rows confirmed it: `messages` 2 bytes, `proxy_server_request`
    7-92 KB carrying the whole conversation. Reading the column alone gives every span a blank
    `system:` line — the single field the trace exists to show."""
    row = {**ROW,
           "messages": {},                       # what the proxy actually writes
           "proxy_server_request": {"body": {
               "messages": [{"role": "system", "content": "you are the umpire"},
                            {"role": "user", "content": "call round R1"}],
               "tools": [{"type": "function", "function": {"name": "rule"}}]}}}
    built = span_from_spend_log(row, realm_id="d", agent_id="umpire")
    assert built is not None
    attrs = built[0]
    assert attrs["bearpit.request.system_prompt"] == "you are the umpire"
    assert attrs["bearpit.request.prompt"] == "user: call round R1"
    assert attrs["bearpit.request.tool_names"] == ["rule"]


def test_the_messages_column_is_still_used_when_the_body_is_absent() -> None:
    """A proxy version that does populate the column must keep working."""
    row = {**ROW, "proxy_server_request": {}}
    built = span_from_spend_log(row, realm_id="d", agent_id="o")
    assert built is not None
    assert built[0]["bearpit.request.system_prompt"] == "You are orin. Win the duel."


def test_the_unwrapped_request_shape_is_what_the_proxy_actually_stores() -> None:
    """Live rows have NO `body` wrapper — this is the shape that reaches the database.

    Inside LiteLLM the request is wrapped (`proxy_server_request["body"]`), but what it PERSISTS is
    the body itself: `{"model": …, "messages": [...], "tools": [...]}`. Following the source rather
    than the stored row produced spans with no prompt and no tools at all, on a 92 KB row that
    contained both. Verified against a real production row afterwards: 2294-char system prompt and
    44 offered tools."""
    row = {**ROW,
           "messages": {},
           "proxy_server_request": {                      # no "body" key — as stored
               "model": "gpt-5.4",
               "messages": [{"role": "system", "content": "# Pong — relay player"},
                            {"role": "user", "content": "your turn"}],
               "tools": [{"type": "function",
                          "function": {"name": "mcp_realmtools_submit_sealed"}}],
               "stream": True}}
    built = span_from_spend_log(row, realm_id="d", agent_id="pong")
    assert built is not None
    attrs = built[0]
    assert attrs["bearpit.request.system_prompt"] == "# Pong — relay player"
    assert attrs["bearpit.request.prompt"] == "user: your turn"
    assert attrs["bearpit.request.tool_names"] == ["mcp_realmtools_submit_sealed"]


def test_both_request_shapes_are_accepted() -> None:
    """The wrapped form must keep working, in case a proxy version stores it that way."""
    wrapped = {**ROW, "messages": {}, "proxy_server_request": {
        "body": {"messages": [{"role": "system", "content": "wrapped"}], "tools": []}}}
    built = span_from_spend_log(wrapped, realm_id="d", agent_id="o")
    assert built is not None
    assert built[0]["bearpit.request.system_prompt"] == "wrapped"
