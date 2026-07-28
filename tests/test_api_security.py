"""Boundary defences on the control-plane origin.

This origin has no authentication (#105) and can start realms, spend money, and read every
transcript, private note and revealed submission. Loopback binding alone does not stop a web page
from reaching it, so three things hold the line and each is asserted here:

  * the final report is served as Markdown, never HTML — it embeds referee-authored strings, which
    are model output produced after reading agent chat;
  * a request whose Host is not ours is refused, which is what breaks DNS rebinding;
  * a state-changing request from another origin is refused, which is what breaks the cross-site
    form POST that /stop and /rerun would otherwise accept.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from test_api import FakeManager

from bearpit.chronicle import Chronicle, EventKind
from bearpit.gatekeeper.api import create_app


@pytest.fixture
async def chron(tmp_path):
    c = await Chronicle.connect(f"sqlite+aiosqlite:///{tmp_path}/c.db", create=True)
    yield c
    await c.close()


def _client(chron: Chronicle) -> TestClient:
    return TestClient(create_app(chron=chron, manager=FakeManager()))


# --- the report is not HTML -----------------------------------------------------------------
async def test_report_is_served_as_markdown_not_html(chron: Chronicle) -> None:
    await chron.append_event("r1", EventKind.LIFECYCLE, {"event": "created"})
    with _client(chron) as c:
        r = c.get("/api/realms/r1/report")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "html" not in r.headers["content-type"]
    assert r.headers.get("x-content-type-options") == "nosniff"


async def test_referee_authored_text_never_becomes_live_html(chron: Chronicle) -> None:
    """A verdict is raw model output written after reading attacker-controlled agent chat. If it
    were served as text/html, one injected outcome would run same-origin JS against this API."""
    payload = {"outcome": '<img src=x onerror="fetch(`/api/realms`)">vela wins'}
    await chron.append_event("r2", EventKind.VERDICT, payload)
    with _client(chron) as c:
        r = c.get("/api/realms/r2/report")

    assert "onerror" in r.text  # the content is preserved verbatim…
    assert not r.headers["content-type"].startswith("text/html")  # …but never executable here


# --- DNS rebinding --------------------------------------------------------------------------
async def test_a_foreign_host_header_is_refused(chron: Chronicle) -> None:
    with _client(chron) as c:
        r = c.get("/api/realms", headers={"Host": "attacker.example"})
    assert r.status_code == 400


async def test_loopback_hosts_are_accepted(chron: Chronicle) -> None:
    with _client(chron) as c:
        for host in ("127.0.0.1:8000", "localhost:8000"):
            assert c.get("/api/realms", headers={"Host": host}).status_code == 200


def test_the_allowlist_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    from bearpit.gatekeeper.api import _allowed_hosts

    monkeypatch.delenv("BEARPIT_ALLOWED_HOSTS", raising=False)
    assert "127.0.0.1" in _allowed_hosts()
    monkeypatch.setenv("BEARPIT_ALLOWED_HOSTS", "realms.internal, 10.0.0.4")
    assert _allowed_hosts() == ["realms.internal", "10.0.0.4"]


# --- cross-site state change ------------------------------------------------------------------
async def test_a_cross_site_post_is_refused(chron: Chronicle) -> None:
    """`/stop` takes no body, so a plain <form> on any page would otherwise reach it."""
    with _client(chron) as c:
        r = c.post("/api/realms/r1/stop", headers={"Origin": "https://attacker.example"})
    assert r.status_code == 403
    assert "cross-site" in r.json()["detail"]


async def test_sec_fetch_site_is_honoured(chron: Chronicle) -> None:
    with _client(chron) as c:
        r = c.post("/api/realms/r1/stop", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


async def test_a_same_origin_write_still_works(chron: Chronicle) -> None:
    """The UI must keep functioning — it is same-origin, so it sends no foreign Origin."""
    with _client(chron) as c:
        same_origin = c.post(
            "/api/realms/r1/stop",
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        )
        no_origin = c.post("/api/realms/r1/stop")
    for r in (same_origin, no_origin):
        assert r.status_code != 403


async def test_reads_are_never_blocked_by_the_cross_site_guard(chron: Chronicle) -> None:
    """GET is safe by definition here; blocking it would break linking to a report."""
    with _client(chron) as c:
        r = c.get("/api/realms", headers={"Origin": "https://elsewhere.example"})
    assert r.status_code == 200
