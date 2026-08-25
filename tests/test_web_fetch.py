"""`web_fetch` and its SSRF defences (#55, ADR-004 §8).

Host-brokering is what makes tool access safe from the container's side and precisely what makes
it dangerous from the host's: the host reaches the LAN, loopback and cloud metadata, and the AGENT
picks the URL. Every defence below has a test that fails without it, because a defence with no
failing test is a comment.

Nothing here touches the network. DNS is substituted, so the tests are about the decision to
connect — which is the thing being defended — rather than about anyone's uptime.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from bearpit.core import webfetch
from bearpit.core.webfetch import WEB_FETCH, FetchRefused, fetch


@pytest.fixture
def dns(monkeypatch):
    """Point every hostname wherever the test says."""
    table: dict[str, list[str]] = {}

    async def resolve(host: str, port: int) -> list[str]:
        if host in table:
            return table[host]
        raise OSError(f"unknown host {host}")

    monkeypatch.setattr(webfetch, "_resolve", resolve)
    return table


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _ok(text: str = "hello", content_type: str = "text/html") -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text, headers={"content-type": content_type})
    return handler


# --- the address, not the name, is what gets checked -------------------------------------------
@pytest.mark.parametrize("addr,label", [
    ("127.0.0.1", "loopback"),
    ("169.254.169.254", "cloud metadata"),
    ("10.0.0.5", "RFC1918"),
    ("192.168.1.1", "home LAN"),
    ("172.16.4.4", "RFC1918 /12"),
    ("::1", "IPv6 loopback"),
    ("fd00::1", "IPv6 unique-local"),
    ("fe80::1", "IPv6 link-local"),
    ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
    ("0.0.0.0", "unspecified"),
])
async def test_a_name_resolving_somewhere_private_is_refused(dns, addr, label):
    """The agent never names an address — it names a host. So the check is on what that host
    RESOLVES to, which is the only thing an attacker cannot dress up."""
    dns["evil.example"] = [addr]
    with pytest.raises(FetchRefused, match="non-public"):
        await fetch("http://evil.example/x", client=_client(_ok()))


async def test_one_private_answer_among_several_is_enough_to_refuse(dns):
    """A name answering with both a public and a private address is not a coincidence; it is the
    arrangement. Refusing only when EVERY answer is private would fetch from the LAN half the
    time and pass its own test the other half."""
    dns["split.example"] = ["93.184.216.34", "127.0.0.1"]
    with pytest.raises(FetchRefused, match="non-public"):
        await fetch("http://split.example/x", client=_client(_ok()))


async def test_a_public_host_is_fetched(dns):
    dns["example.org"] = ["93.184.216.34"]
    out = await fetch("http://example.org/page", client=_client(_ok("the page")))
    assert out["text"] == "the page" and out["status"] == 200


# --- scheme, credentials, allowlist ------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://x/", "ftp://x/", "data:text/plain,hi",
])
async def test_only_http_and_https_are_fetchable(dns, url):
    with pytest.raises(FetchRefused, match="only http"):
        await fetch(url, client=_client(_ok()))


async def test_credentials_in_the_url_are_refused(dns):
    """Ambient authority an agent composed. Also a classic parser-confusion vector."""
    dns["example.org"] = ["93.184.216.34"]
    with pytest.raises(FetchRefused, match="[Cc]redentials"):
        await fetch("http://user:pw@example.org/", client=_client(_ok()))


async def test_the_scenarios_allowlist_is_enforced(dns):
    dns["evil.example"] = ["93.184.216.34"]
    dns["en.wikipedia.org"] = ["93.184.216.34"]
    with pytest.raises(FetchRefused, match="allowed hosts"):
        await fetch("http://evil.example/x", allow=["*.wikipedia.org"], client=_client(_ok()))
    out = await fetch("http://en.wikipedia.org/x", allow=["*.wikipedia.org"],
                      client=_client(_ok("wiki")))
    assert out["text"] == "wiki"


# --- redirects ---------------------------------------------------------------------------------
async def test_a_redirect_to_a_private_address_is_refused(dns):
    """THE bypass. A permitted host answering 302 -> 127.0.0.1 defeats any check that only looks
    at the URL the agent supplied."""
    dns["public.example"] = ["93.184.216.34"]
    dns["internal.example"] = ["127.0.0.1"]

    def handler(request: httpx.Request) -> httpx.Response:
        # dispatch on the Host header: the URL now carries the pinned ADDRESS
        if request.headers.get("host") == "public.example":
            return httpx.Response(302, headers={"location": "http://internal.example/secrets"})
        return httpx.Response(200, text="SECRETS", headers={"content-type": "text/plain"})

    with pytest.raises(FetchRefused, match="non-public"):
        await fetch("http://public.example/", client=_client(handler))


async def test_a_redirect_chain_is_capped_and_recorded(dns):
    dns["a.example"] = ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://a.example/next"})

    with pytest.raises(FetchRefused, match="redirected more than"):
        await fetch("http://a.example/", client=_client(handler))


async def test_the_redirect_chain_is_reported(dns):
    """What was actually fetched is not what the agent asked for; the record has to say both."""
    dns["a.example"] = ["93.184.216.34"]
    dns["b.example"] = ["93.184.216.34"]
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if request.headers.get("host") == "a.example":
            return httpx.Response(301, headers={"location": "http://b.example/final"})
        return httpx.Response(200, text="arrived", headers={"content-type": "text/plain"})

    out = await fetch("http://a.example/start", client=_client(handler))
    assert out["text"] == "arrived"
    assert out["redirects"] == ["http://a.example/start"]
    assert out["url"] == "http://b.example/final"


async def test_a_redirect_with_no_location_is_refused(dns):
    dns["a.example"] = ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    with pytest.raises(FetchRefused, match="without saying where"):
        await fetch("http://a.example/", client=_client(handler))


# --- what comes back ---------------------------------------------------------------------------
async def test_binary_content_is_refused_rather_than_mangled(dns):
    dns["a.example"] = ["93.184.216.34"]
    with pytest.raises(FetchRefused, match="only text"):
        await fetch("http://a.example/x", client=_client(_ok("\x00\x01", "image/png")))


@pytest.mark.parametrize("content_type", [
    "text/html; charset=utf-8", "application/json", "application/xml",
    "application/ld+json", "text/plain",
])
async def test_readable_types_are_accepted(dns, content_type):
    dns["a.example"] = ["93.184.216.34"]
    out = await fetch("http://a.example/x", client=_client(_ok("body", content_type)))
    assert out["text"] == "body"


async def test_a_huge_response_is_truncated_and_says_so(dns):
    """An agent's context is finite and the chronicle is append-only. A 50 MB page must cost
    neither."""
    dns["a.example"] = ["93.184.216.34"]
    out = await fetch("http://a.example/x",
                      client=_client(_ok("x " * webfetch.MAX_TEXT_CHARS, "text/plain")))
    assert len(out["text"]) == webfetch.MAX_TEXT_CHARS
    assert out["truncated"] is True


# --- no inherited authority --------------------------------------------------------------------
async def test_no_cookies_or_ambient_proxy_credentials_travel_with_the_request(dns):
    """The request is composed by an agent. Whatever the platform happens to be authenticated to
    must not ride along with it."""
    dns["a.example"] = ["93.184.216.34"]
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    await fetch("http://a.example/x", client=_client(handler))
    lowered = {k.lower() for k in captured["headers"]}
    assert "cookie" not in lowered
    assert "authorization" not in lowered
    assert "proxy-authorization" not in lowered


# --- the tool profile --------------------------------------------------------------------------
async def test_the_handler_returns_an_error_rather_than_raising(dns):
    """A refusal must reach the agent as something it can read and route around, not as a crash."""
    dns["evil.example"] = ["127.0.0.1"]
    out = await WEB_FETCH.handler({"url": "http://evil.example/"}, {}, None)
    assert "non-public" in out["error"]


async def test_the_handler_takes_its_allowlist_from_the_scenario(dns):
    dns["evil.example"] = ["93.184.216.34"]
    out = await WEB_FETCH.handler({"url": "http://evil.example/"},
                                  {"allow": ["*.wikipedia.org"]}, None)
    assert "allowed hosts" in out["error"]


async def test_a_missing_url_is_answered_not_crashed():
    assert "needs a url" in (await WEB_FETCH.handler({}, {}, None))["error"]


def test_web_fetch_is_a_builtin_and_contained():
    """It needs no key and no third party, so the platform should be useful without an install."""
    from bearpit.core.tools import ToolRisk, tool_registry

    profile = tool_registry()["web_fetch"]   # present with no plugin installed
    assert profile.risk is ToolRisk.CONTAINED
    assert profile.api_key_ref is None, "a built-in must not need a key to be useful"


# --- the connection goes to the address that was validated -------------------------------------
async def test_the_request_connects_to_the_validated_address_not_the_name(dns):
    """The defence the rest of this file rests on.

    Validating what a name resolves to and then asking the HTTP client for that NAME re-resolves
    at connect time, and the second lookup can answer differently from the first. That is DNS
    rebinding, and it silently turns every check above into advice. An earlier version of this
    module computed the address, discarded it, and requested by hostname — with a docstring
    claiming otherwise, and every other test in this file still passing.
    """
    dns["example.org"] = ["93.184.216.34"]
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url_host"] = request.url.host
        seen["host_header"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    await fetch("http://example.org/page", client=_client(handler))
    assert seen["url_host"] == "93.184.216.34", "the connection was made by name, not by address"
    assert seen["host_header"] == "example.org", "virtual hosting and TLS need the real name"
    assert seen["sni"] == "example.org"


async def test_a_relative_redirect_is_resolved_against_the_name_not_the_address(dns):
    """Joining a relative Location against the pinned URL would produce a bare-IP request, which
    skips the hostname allowlist entirely — a redirect could then walk out of the allowed set."""
    dns["en.wikipedia.org"] = ["93.184.216.34"]
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers.get("host", ""))
        if len(hosts) == 1:
            return httpx.Response(302, headers={"location": "/wiki/Otter"})
        return httpx.Response(200, text="otter", headers={"content-type": "text/plain"})

    out = await fetch("http://en.wikipedia.org/start", allow=["*.wikipedia.org"],
                      client=_client(handler))
    assert out["text"] == "otter"
    assert hosts == ["en.wikipedia.org", "en.wikipedia.org"]
    assert out["url"] == "http://en.wikipedia.org/wiki/Otter"


# --- identifying ourselves ---------------------------------------------------------------------
async def test_every_request_carries_a_descriptive_user_agent(dns):
    """Not politeness — a requirement.

    Wikimedia's robot policy answers 403 to a request whose User-Agent is a bare client default,
    and that is what the HTTP client sends unless told otherwise. Live, an agent granted this tool
    fetched Wikipedia three times, got three 403s, and diagnosed the cause itself. The scenario's
    allowlist was `*.wikipedia.org`, so the one host it could reach was the one refusing us.
    """
    dns["en.wikipedia.org"] = ["93.184.216.34"]
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    await fetch("https://en.wikipedia.org/wiki/Iceland", client=_client(handler))
    ua = seen["ua"]
    assert "Bearpit" in ua, f"no identifying User-Agent was sent: {ua!r}"
    assert "http" in ua, "Wikimedia's policy asks for a URL or contact in the User-Agent"
    assert "python-httpx" not in ua, "the client default is what gets 403'd"


async def test_the_user_agent_survives_a_redirect(dns):
    """Each hop is a fresh request built by hand, so the header has to be set on every one."""
    dns["a.example"] = ["93.184.216.34"]
    dns["b.example"] = ["93.184.216.34"]
    agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        agents.append(request.headers.get("user-agent", ""))
        if request.headers.get("host") == "a.example":
            return httpx.Response(302, headers={"location": "http://b.example/final"})
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    await fetch("http://a.example/start", client=_client(handler))
    assert len(agents) == 2
    assert all("Bearpit" in ua for ua in agents)


# --- what comes back must be readable, not excavated (#77) -------------------------------------
_PAGE = """<!DOCTYPE html>
<html><head><title>Iceland</title>
<script>var tracking = {a:1}; document.write("noise");</script>
<style>.vector-header { display: none; }</style>
</head><body>
<nav>Main page  Contents  Random article</nav>
<p>Reykjavik is home to about 35% of the country's roughly <b>395,000</b> residents.</p>
<p>PADDING. """ + ("Filler sentence that stands between the two facts. " * 40) + """</p>
<p>The island is volcanically active.</p>
<!-- a comment nobody needs -->
</body></html>"""


async def test_html_comes_back_as_prose_not_markup(dns):
    """The root cause of #77. An agent fetching an article received 256 KB of raw HTML — ~64k
    tokens, 4,677 tags — and reasonably went on writing from memory instead. Content a model has
    to excavate is not content it can quote."""
    dns["en.wikipedia.org"] = ["93.184.216.34"]
    out = await fetch("https://en.wikipedia.org/wiki/Iceland",
                      client=_client(_ok(_PAGE, "text/html")))
    text = out["text"]
    assert "<p>" not in text and "<html" not in text, "markup reached the agent"
    assert "var tracking" not in text, "script bodies are not content"
    assert "display: none" not in text, "stylesheets are not content"
    assert "a comment nobody needs" not in text
    assert "roughly 395,000 residents" in text, "the actual fact must survive extraction"
    assert out["extracted"] is True


async def test_json_and_plain_text_are_left_exactly_alone(dns):
    """An API response is already the good case — parsing it as HTML would corrupt it."""
    dns["api.example"] = ["93.184.216.34"]
    raw = '{"population": 395000, "note": "a < b && c > d"}'
    out = await fetch("https://api.example/x", client=_client(_ok(raw, "application/json")))
    assert out["text"] == raw
    assert out["extracted"] is False


async def test_the_TEXT_is_what_gets_truncated_not_the_raw_bytes(dns):
    """1.5 MB of markup cut to 256 KB of markup can discard the whole article and keep the nav.
    Extract first, then bound what the agent actually reads."""
    dns["big.example"] = ["93.184.216.34"]
    filler = "<div class='x'><span>" + ("padding " * 400) + "</span></div>"
    page = "<html><body>" + (filler * 400) + "<p>THE FIGURE IS 395,000</p></body></html>"
    out = await fetch("https://big.example/x", client=_client(_ok(page, "text/html")))
    assert out["bytes"] > webfetch.MAX_TEXT_CHARS, "this page needs to be big enough to matter"
    assert len(out["text"]) <= webfetch.MAX_TEXT_CHARS
    assert out["truncated"] is True


async def test_contains_returns_the_passages_around_a_match(dns):
    """So an agent can find a figure instead of scanning tens of thousands of tokens — and what
    comes back is short enough to quote verbatim, which is the behaviour #77 wants."""
    dns["en.wikipedia.org"] = ["93.184.216.34"]
    out = await fetch("https://en.wikipedia.org/wiki/Iceland", contains="395,000",
                      client=_client(_ok(_PAGE, "text/html")))
    assert "roughly 395,000 residents" in out["text"]
    assert "volcanically active" not in out["text"], "only the matching passages come back"
    assert out["matched"] == 1


async def test_contains_says_so_when_it_finds_nothing(dns):
    """Silence would read as 'the page does not say that'. It has to be distinguishable from a
    page that was never searched."""
    dns["en.wikipedia.org"] = ["93.184.216.34"]
    out = await fetch("https://en.wikipedia.org/wiki/Iceland", contains="banana",
                      client=_client(_ok(_PAGE, "text/html")))
    assert out["matched"] == 0
    assert "banana" in out["note"] and "not found" in out["note"]
    assert "395,000" in out["text"], "fall back to the page, so the turn is not wasted"


async def test_contains_is_case_insensitive_and_matches_across_whitespace(dns):
    """Extraction collapses whitespace, and a model types a phrase the way it reads, not the way
    the markup happened to break lines."""
    dns["en.wikipedia.org"] = ["93.184.216.34"]
    out = await fetch("https://en.wikipedia.org/wiki/Iceland", contains="REYKJAVIK IS HOME",
                      client=_client(_ok(_PAGE, "text/html")))
    assert out["matched"] == 1


async def test_the_handler_passes_contains_through(monkeypatch):
    """The tool declares `contains`, and what the agent sends has to reach `fetch` — a parameter
    advertised and then dropped is worse than one never offered."""
    seen: dict[str, Any] = {}

    async def spy(url: str, **kw: Any) -> dict[str, Any]:
        seen.update({"url": url, **kw})
        return {"text": "ok", "matched": 1}

    monkeypatch.setattr(webfetch, "fetch", spy)
    await WEB_FETCH.handler(
        {"url": "https://en.wikipedia.org/wiki/Iceland", "contains": "395,000"}, {}, None)
    assert "contains" in WEB_FETCH.params["properties"], "the agent is never told it exists"
    assert seen["contains"] == "395,000"

    seen.clear()
    await WEB_FETCH.handler({"url": "https://en.wikipedia.org/wiki/Iceland"}, {}, None)
    assert seen["contains"] is None, "an omitted argument must not become an empty search"
