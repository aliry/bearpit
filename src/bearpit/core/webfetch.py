"""`web_fetch` — the built-in tool, and the defences that make host-brokering safe (ADR-004 §8).

Brokering is what makes tool access safe from the *container's* point of view, and it is precisely
what makes it dangerous from the *host's*. The host can reach the operator's LAN, the control plane
on loopback, and cloud metadata endpoints that need no credentials at all — and **the agent chooses
the URL**. An agent asking for `http://169.254.169.254/latest/meta-data/` is not a hypothetical; it
is the first thing anyone tries.

So the address, not the hostname, is the thing that gets validated, and it is validated again after
every redirect. Resolving a name and then handing that name to an HTTP client re-resolves it, which
reopens DNS rebinding: the second lookup can answer differently from the first. This module
resolves once, checks what it got, and connects to **that address**, carrying the original `Host`
header so virtual hosting still works.

Everything here runs on the host, under the platform's own network identity. Nothing in this file
ever executes inside an agent container.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from fnmatch import fnmatch
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from bearpit.core.tools import ToolProfile, ToolRisk

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 3
TIMEOUT_S = 10.0
# Identify the platform, with a URL someone can look up.
#
# Not politeness — a requirement. Wikimedia's robot policy returns 403 to a request whose
# User-Agent is a bare client default, and the HTTP client's default is exactly that. Live, an
# agent granted `web_fetch` fetched Wikipedia three times, got three 403s, and diagnosed the cause
# itself: "Wikipedia's robot policy is blocking requests without a proper user-agent". A tool
# whose most obvious research target refuses it is a tool that does not work.
USER_AGENT = (
    "Bearpit/0.1 (+https://github.com/aliry/bearpit; agent research tool; contact via repo)"
)
MAX_BYTES = 4 * 1024 * 1024   # what we will read off the wire; mostly markup, and cheap
# What the AGENT actually reads, applied AFTER extraction. The old code capped raw bytes at 256 KB,
# so a 1.5 MB article arrived as 256 KB of truncated markup — ~64k tokens of tags with the content
# cut off. Extracting first means this budget is spent on prose (#77).
MAX_TEXT_CHARS = 40_000
CONTEXT_CHARS = 600           # how much of a passage `contains` returns around each hit
MAX_MATCHES = 8
ALLOWED_CONTENT = ("text/", "application/json", "application/xml", "application/xhtml",
                   "+json", "+xml")


class _TextExtractor(HTMLParser):
    """HTML -> the prose a reader would see.

    Stdlib rather than a parsing library: this runs on the host against pages an agent chose, so
    the smaller its dependency surface the better, and "drop the tags, keep the words" needs no
    more than this. It is not a renderer and does not try to be.
    """

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    _BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "section", "article", "header", "footer", "table", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BREAK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # collapse runs of spaces within a line, and runs of blank lines between them
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n+", "\n\n", joined)
        return joined.strip()


def extract_text(html: str) -> str:
    """Prose from HTML. Returns the input unchanged if it cannot be parsed at all."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed page must degrade, never raise
        log.warning("could not parse a fetched page as HTML; returning it raw")
        return html
    return parser.text() or html


def find_passages(text: str, needle: str) -> tuple[str, int]:
    """The passages around each occurrence of `needle`, and how many there were.

    Exists so an agent can locate a figure rather than scan tens of thousands of tokens — and so
    what comes back is short enough to quote verbatim, which is what turns a fetch into a citation.
    """
    hay, want = text.lower(), needle.lower().strip()
    if not want:
        return text, 0
    spans: list[tuple[int, int]] = []
    start = 0
    while len(spans) < MAX_MATCHES:
        hit = hay.find(want, start)
        if hit < 0:
            break
        spans.append((max(0, hit - CONTEXT_CHARS), min(len(text), hit + len(want) + CONTEXT_CHARS)))
        start = hit + len(want)
    if not spans:
        return text, 0
    merged: list[list[int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return "\n\n…\n\n".join(text[lo:hi].strip() for lo, hi in merged), len(spans)


class FetchRefused(Exception):
    """The request was refused before anything left this machine."""


def _is_public(ip: str) -> bool:
    """False for every address that is not a normal, routable, public destination.

    Deliberately an allowlist of one property (`is_global`) plus explicit exclusions, rather than a
    list of bad ranges: a blocklist of ranges is a list someone forgets to extend, and IPv6 gives
    them plenty of chances.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
            or addr.is_reserved or addr.is_unspecified):
        return False
    # IPv4-mapped/compatible IPv6 (::ffff:127.0.0.1) would otherwise sail past the checks above
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return _is_public(str(mapped))
    return bool(addr.is_global)


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, proto=socket.IPPROTO_TCP,
    )
    return [str(info[4][0]) for info in infos]


def _host_allowed(host: str, allow: list[str]) -> bool:
    """`allow` is a scenario-supplied list of host patterns; empty means no restriction."""
    return not allow or any(fnmatch(host.lower(), pattern.lower()) for pattern in allow)


async def _vet(url: str, allow: list[str]) -> tuple[str, str, str]:
    """Validate one URL. Returns (connect_ip, host_header, rebuilt_url), or raises FetchRefused."""
    parts = urlparse(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise FetchRefused(f"only {' and '.join(ALLOWED_SCHEMES)} URLs may be fetched")
    if parts.username or parts.password:
        raise FetchRefused("credentials in the URL are not allowed")
    host = parts.hostname or ""
    if not host:
        raise FetchRefused("that URL has no host")
    if not _host_allowed(host, allow):
        raise FetchRefused(f"{host!r} is not in this scenario's allowed hosts")

    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = await _resolve(host, port)
    except OSError as exc:
        raise FetchRefused(f"could not resolve {host!r} ({exc})") from exc
    if not addresses:
        raise FetchRefused(f"could not resolve {host!r}")
    # EVERY answer must be public. One private address among several is enough to refuse: a name
    # that resolves to both is exactly what an attacker arranges.
    bad = [a for a in addresses if not _is_public(a)]
    if bad:
        raise FetchRefused(
            f"{host!r} resolves to a non-public address ({bad[0]}) — the platform will not fetch "
            f"from inside its own network"
        )
    # Rebuild the URL against the ADDRESS we just validated, keeping the hostname for the `Host`
    # header and TLS. Requesting by name would re-resolve at connect time, and the second lookup
    # can answer differently from the first — which is DNS rebinding, and would make every check
    # above advisory. Verified against a live host: connecting to the IP with `sni_hostname` set
    # still gets full certificate verification for the NAME.
    literal = f"[{addresses[0]}]" if ":" in addresses[0] else addresses[0]
    netloc = f"{literal}:{parts.port}" if parts.port else literal
    pinned = urlunparse(parts._replace(netloc=netloc))
    return addresses[0], host, pinned


async def fetch(url: str, *, allow: list[str] | None = None, contains: str | None = None,
                client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Fetch a public URL as readable text, re-validating every redirect hop.

    HTML comes back as prose, not markup, and `contains` narrows it to the passages around a
    match — both so that what an agent receives is something it can quote (#77).
    """
    allowed = list(allow or [])
    hops: list[str] = []
    current = url
    owns_client = client is None
    # No cookie jar, no auth, no environment proxies: the platform's ambient authority must not
    # travel with a request an agent composed.
    http = client or httpx.AsyncClient(
        follow_redirects=False, timeout=TIMEOUT_S, trust_env=False, cookies=None,
    )
    try:
        for _ in range(MAX_REDIRECTS + 1):
            _ip, host, pinned = await _vet(current, allowed)
            hops.append(current)   # record what was ASKED for, not the address it resolved to
            response = await http.get(
                pinned,
                headers={
                    "Host": host,
                    "Accept": "text/*, application/json;q=0.9",
                    "User-Agent": USER_AGENT,
                },
                extensions={"sni_hostname": host},
            )
            if response.is_redirect:
                location = response.headers.get("location", "")
                if not location:
                    raise FetchRefused("that URL redirected without saying where")
                # resolve the Location against the ORIGINAL url: joining against the pinned one
                # would turn a relative redirect into a bare-IP request that skips `_vet`'s
                # hostname checks entirely.
                current = str(httpx.URL(current).join(location))
                continue

            content_type = response.headers.get("content-type", "")
            if not any(marker in content_type for marker in ALLOWED_CONTENT):
                raise FetchRefused(
                    f"that URL returned {content_type or 'an unknown type'}; only text, JSON and "
                    f"XML can be read"
                )
            raw = response.content[:MAX_BYTES].decode(response.encoding or "utf-8", "replace")
            is_html = ("html" in content_type
                       or raw.lstrip()[:14].lower().startswith("<!doctype html"))
            body = extract_text(raw) if is_html else raw

            note = ""
            matched = 0
            if contains:
                narrowed, matched = find_passages(body, contains)
                if matched:
                    body = narrowed
                else:
                    # Distinguishable from a page nobody searched, and the page still comes back
                    # so the turn is not spent on nothing.
                    note = (f"{contains!r} was not found on this page — showing the page instead; "
                            f"the wording may differ, or the figure may be elsewhere")

            full = len(body)
            if full > MAX_TEXT_CHARS:
                body = body[:MAX_TEXT_CHARS]
            return {
                "url": hops[-1], "status": response.status_code, "redirects": hops[:-1],
                "bytes": len(response.content),
                "truncated": full > MAX_TEXT_CHARS or len(response.content) > MAX_BYTES,
                "content_type": content_type, "extracted": is_html,
                "matched": matched, "note": note, "text": body,
            }
        raise FetchRefused(f"that URL redirected more than {MAX_REDIRECTS} times")
    finally:
        if owns_client:
            await http.aclose()


async def _handler(args: dict[str, Any], config: dict[str, Any], ctx: Any) -> Any:
    url = str(args.get("url") or "").strip()
    if not url:
        return {"error": "web_fetch needs a url"}
    allow = config.get("allow")
    contains = str(args.get("contains") or "").strip() or None
    try:
        return await fetch(url, allow=list(allow) if isinstance(allow, list) else None,
                           contains=contains)
    except FetchRefused as exc:
        # Readable, and deliberately specific: the agent should be able to choose a different URL
        # rather than retry the same one.
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"could not fetch that URL ({type(exc).__name__})"}


WEB_FETCH = ToolProfile(
    name="web_fetch",
    label="Fetch a web page",
    description=(
        "Fetch a public web page or JSON document and read it as text. Give the full URL, "
        "including https://. HTML is returned as readable prose, not markup. Pass `contains` with "
        "a word or phrase you expect on the page and only the passages around it come back — use "
        "it to find a figure, and quote what it returns. Only public internet addresses can be "
        "reached, and only text, JSON or XML is returned."
    ),
    params={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full URL to fetch."},
            "contains": {
                "type": "string",
                "description": "Optional. A word or phrase to locate on the page; only the "
                               "passages around it are returned, short enough to quote.",
            },
        },
        "required": ["url"],
    },
    config_schema={
        "type": "object",
        "properties": {
            "allow": {
                "type": "array", "items": {"type": "string"},
                "description": "Host patterns this scenario permits, e.g. '*.wikipedia.org'.",
            },
            "max_calls_per_agent": {"type": "integer", "minimum": 0},
            "max_calls_by_agent": {
                "type": "object", "additionalProperties": {"type": "integer", "minimum": 0},
                "description": "Per-agent overrides, e.g. {'critic': 24} — a verifier that "
                               "re-checks others' sources needs more calls than they do.",
            },
        },
        "additionalProperties": False,
    },
    handler=_handler,
    risk=ToolRisk.CONTAINED,
    cost_per_call_usd=0.0,
)


__all__ = ["ALLOWED_SCHEMES", "MAX_BYTES", "MAX_REDIRECTS", "MAX_TEXT_CHARS", "TIMEOUT_S",
           "USER_AGENT", "WEB_FETCH", "FetchRefused", "extract_text", "fetch", "find_passages"]
