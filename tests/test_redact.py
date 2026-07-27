"""Masking platform-minted secrets before they are recorded.

`run_code` runs inside the agent's own container, which holds its Matrix access token and its
LiteLLM virtual key in plaintext. An agent that prints its environment would otherwise write live,
replayable credentials into an append-only log that is served over the API and included in exports.
"""

from __future__ import annotations

from agentrealm.core.redact import MASK, Redactor, redact

TOKEN = "syt_dmVsYQ_QWERTYuiopASDFGHjkl_2xY9Zq"
VKEY = "sk-litellm-3f9a2c7e51b04d8ea6c1f0d2b7e4a913"


def test_a_known_secret_is_masked() -> None:
    out = redact(f"MATRIX_ACCESS_TOKEN={TOKEN}", [TOKEN])
    assert TOKEN not in out
    assert out == f"MATRIX_ACCESS_TOKEN={MASK}"


def test_every_occurrence_goes() -> None:
    text = f"{TOKEN} ... again {TOKEN}"
    assert redact(text, [TOKEN]).count(MASK) == 2
    assert TOKEN not in redact(text, [TOKEN])


def test_a_full_env_dump_is_scrubbed() -> None:
    """The realistic case: an agent runs `env` and pipes the lot back."""
    dump = "\n".join([
        "HOME=/home/agent",
        f"MATRIX_ACCESS_TOKEN={TOKEN}",
        f"MODEL_API_KEY={VKEY}",
        "MATRIX_USER_ID=@vela:realm.local",
    ])
    out = redact(dump, [TOKEN, VKEY])
    assert TOKEN not in out and VKEY not in out
    assert "HOME=/home/agent" in out            # ordinary output survives untouched
    assert "@vela:realm.local" in out           # an identity is not a credential


def test_overlapping_secrets_leave_no_fragment() -> None:
    """A bearer header contains the raw token. Masking the shorter one first would leave the
    header's surrounding text holding a partial credential."""
    header = f"Authorization: Bearer {TOKEN}"
    out = redact(f"argv={header}", [TOKEN, header])
    assert TOKEN not in out


def test_short_and_empty_values_are_ignored() -> None:
    """Masking "id" or "" would corrupt ordinary text without protecting anything."""
    text = "id=7 and no errors"
    assert redact(text, ["", None, "id", "no"]) == text


def test_nothing_to_redact_returns_the_text_unchanged() -> None:
    assert redact("all clear", []) == "all clear"
    assert redact("", [TOKEN]) == ""


def test_redactor_is_falsey_when_it_holds_nothing_usable() -> None:
    assert not Redactor([])
    assert not Redactor(["", None, "abc"])      # all below the length floor
    assert Redactor([TOKEN])


def test_redactor_is_reusable() -> None:
    r = Redactor([TOKEN, VKEY])
    assert TOKEN not in r(f"first {TOKEN}")
    assert VKEY not in r(f"second {VKEY}")
