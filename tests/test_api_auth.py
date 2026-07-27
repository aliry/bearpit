"""The local API token.

The control-plane origin can start realms, spend real money, read every transcript and private
note, and take an arbitrary host path as a scenario package. Loopback binding stops the network but
not the rest of the machine, so /api/* requires a token.

The awkward part of any such scheme is the browser, and these tests pin the answer: the console
page is public, `/?token=…` exchanges the token for a cookie, and the console then works with no
login form. Get that wrong in either direction — gating `/` so the exchange can never happen, or
leaving a write route open — and the whole thing is either unusable or pointless.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from test_api import FakeManager

from agentrealm.chronicle import Chronicle, EventKind
from agentrealm.gatekeeper.api import create_app
from agentrealm.gatekeeper.auth import (
    COOKIE_NAME,
    is_public_path,
    load_or_create_token,
    presented_token,
    token_matches,
)

TOKEN = "test-token-not-a-real-one"


@pytest.fixture
async def chron(tmp_path):
    c = await Chronicle.connect(f"sqlite+aiosqlite:///{tmp_path}/c.db", create=True)
    await c.append_event("r1", EventKind.LIFECYCLE, {"event": "created"})
    yield c
    await c.close()


def _client(chron: Chronicle, token: str | None = TOKEN) -> TestClient:
    return TestClient(create_app(chron=chron, manager=FakeManager(), auth_token=token))


# --- the gate ---------------------------------------------------------------------------------
async def test_api_without_a_token_is_refused(chron: Chronicle) -> None:
    with _client(chron) as c:
        r = c.get("/api/realms")
    assert r.status_code == 401
    assert "token" in r.json()["detail"]


async def test_a_wrong_token_is_refused(chron: Chronicle) -> None:
    with _client(chron) as c:
        r = c.get("/api/realms", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_a_bearer_token_is_accepted(chron: Chronicle) -> None:
    with _client(chron) as c:
        r = c.get("/api/realms", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


async def test_write_routes_are_gated_too(chron: Chronicle) -> None:
    """The routes that spend money and start containers are the reason this exists."""
    with _client(chron) as c:
        assert c.post("/api/realms/r1/stop").status_code == 401
        assert c.put("/api/settings/provider", json={"model_provider": "azure"}).status_code == 401


# --- the browser path -------------------------------------------------------------------------
async def test_the_console_page_is_public_so_the_exchange_can_happen(chron: Chronicle) -> None:
    """Gating `/` would make the token un-presentable from a browser: there would be nowhere to
    hand it over."""
    with _client(chron) as c:
        assert c.get("/").status_code == 200


async def test_a_token_in_the_url_becomes_a_cookie(chron: Chronicle) -> None:
    with _client(chron) as c:
        r = c.get(f"/?token={TOKEN}")
        assert r.status_code == 200
        assert COOKIE_NAME in r.cookies or COOKIE_NAME in c.cookies
        # …and the console now works without repeating the token anywhere
        assert c.get("/api/realms").status_code == 200


async def test_a_wrong_token_in_the_url_sets_no_cookie(chron: Chronicle) -> None:
    with _client(chron) as c:
        c.get("/?token=wrong")
        assert c.get("/api/realms").status_code == 401


async def test_static_assets_load_before_the_cookie_exists(chron: Chronicle) -> None:
    """The console shell has to fetch its own JS and CSS to run at all."""
    with _client(chron) as c:
        assert c.get("/static/app.js").status_code == 200


# --- opt-out for tests and embedders -----------------------------------------------------------
async def test_no_token_configured_means_no_gate(chron: Chronicle) -> None:
    """How the rest of the suite builds an app — and how someone fronting this with their own
    auth would run it."""
    with _client(chron, token=None) as c:
        assert c.get("/api/realms").status_code == 200


# --- the helpers ------------------------------------------------------------------------------
def test_presented_token_reads_all_three_channels() -> None:
    assert presented_token(header="Bearer abc", cookie=None, query=None) == "abc"
    assert presented_token(header="bearer abc", cookie=None, query=None) == "abc"
    assert presented_token(header=None, cookie="abc", query=None) == "abc"
    assert presented_token(header=None, cookie=None, query="abc") == "abc"
    assert presented_token(header="Basic abc", cookie=None, query=None) is None
    assert presented_token(header=None, cookie=None, query=None) is None


def test_token_comparison_rejects_empty_and_wrong() -> None:
    assert token_matches("secret", "secret")
    assert not token_matches("secret", "secre")
    assert not token_matches("secret", "")
    assert not token_matches("secret", None)


def test_public_paths_are_exactly_the_shell_and_its_assets() -> None:
    assert is_public_path("/")
    assert is_public_path("/static/app.js")
    assert is_public_path("/openapi.json")
    assert not is_public_path("/api/realms")
    assert not is_public_path("/api/settings")


def test_the_token_persists_and_the_env_wins(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTREALM_API_TOKEN", raising=False)
    first = load_or_create_token()
    assert len(first) >= 32
    assert load_or_create_token() == first  # stable across restarts, or the cookie breaks nightly

    stored = tmp_path / ".agentrealm" / "api-token"
    assert stored.is_file()
    assert oct(stored.stat().st_mode & 0o777) == "0o600"

    monkeypatch.setenv("AGENTREALM_API_TOKEN", "pinned-by-the-operator")
    assert load_or_create_token() == "pinned-by-the-operator"
