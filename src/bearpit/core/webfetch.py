"""`web.fetch` — the built-in tool, and the defences that make host-brokering safe (ADR-004 §8).

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
import socket
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from bearpit.core.tools import ToolProfile, ToolRisk

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 3
TIMEOUT_S = 10.0
MAX_BYTES = 256 * 1024
ALLOWED_CONTENT = ("text/", "application/json", "application/xml", "application/xhtml",
                   "+json", "+xml")


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


async def fetch(url: str, *, allow: list[str] | None = None,
                client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Fetch a public URL as text, following redirects manually so each hop is re-validated."""
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
                pinned, headers={"Host": host, "Accept": "text/*, application/json;q=0.9"},
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
            body = response.content[:MAX_BYTES].decode(response.encoding or "utf-8", "replace")
            return {
                "url": hops[-1], "status": response.status_code, "redirects": hops[:-1],
                "bytes": len(response.content), "truncated": len(response.content) > MAX_BYTES,
                "content_type": content_type, "text": body,
            }
        raise FetchRefused(f"that URL redirected more than {MAX_REDIRECTS} times")
    finally:
        if owns_client:
            await http.aclose()


async def _handler(args: dict[str, Any], config: dict[str, Any], ctx: Any) -> Any:
    url = str(args.get("url") or "").strip()
    if not url:
        return {"error": "web.fetch needs a url"}
    allow = config.get("allow")
    try:
        return await fetch(url, allow=list(allow) if isinstance(allow, list) else None)
    except FetchRefused as exc:
        # Readable, and deliberately specific: the agent should be able to choose a different URL
        # rather than retry the same one.
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"could not fetch that URL ({type(exc).__name__})"}


WEB_FETCH = ToolProfile(
    name="web.fetch",
    label="Fetch a web page",
    description=(
        "Fetch a public web page or JSON document and read it as text. Give the full URL, "
        "including https://. Only public internet addresses can be reached, and only text, JSON "
        "or XML is returned."
    ),
    params={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The full URL to fetch."}},
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
        },
        "additionalProperties": False,
    },
    handler=_handler,
    risk=ToolRisk.CONTAINED,
    cost_per_call_usd=0.0,
)


__all__ = ["ALLOWED_SCHEMES", "MAX_BYTES", "MAX_REDIRECTS", "TIMEOUT_S", "WEB_FETCH",
           "FetchRefused", "fetch"]
