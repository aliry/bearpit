"""Agent tool grants — the registry and the plugin contract (ADR-004).

A *tool* is a capability an agent can be granted individually: `web_search`, `web_fetch`, and
whatever an installed package contributes. Any package can contribute one by declaring an entry
point in the `bearpit.tools` group, exactly as a provider plugin contributes a model pipeline
through `bearpit.providers` — including that seam's load-bearing rule: **a plugin that fails to
import, or raises, is logged and skipped. A third-party package must never be able to stop the
platform from starting.**

Two things about this module are deliberate and easy to get wrong later.

**Where each kind of validation lives.** The schema validates the *shape* of a grant and the
manifest's internal consistency; this module validates *existence* — whether the tool is actually
installed, whether its config satisfies its own schema, whether its key is present. The split is
the one `SkillRef` already uses, and it is not stylistic: existence depends on which packages
happen to be installed on this machine, so folding it into the model would make a scenario that
grants `web_search` fail to *load* wherever that plugin is absent — unviewable, uneditable and
unexportable, not merely unlaunchable.

**A name collision is refused, not resolved.** Provider profiles merge last-wins, which suits
data. A tool is behaviour: letting a package installed later silently take over a name would
change what an agent *does* with no manifest edit and nothing said. First registration wins,
collisions are logged, and built-ins cannot be shadowed at all.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import jsonschema

if TYPE_CHECKING:  # avoid a circular import: schema imports nothing from here at runtime
    from bearpit.core.schema import Project

TOOL_GROUP = "bearpit.tools"

# `family_verb`, lowercase, underscore-separated.
#
# NOT `family.verb`, which is what ADR-004 originally specified. A dot survives MCP perfectly well
# — the SDK lists and calls it — and then dies one layer further on: model function-calling APIs
# constrain a tool name to [A-Za-z0-9_-], so a dotted tool never reaches the model at all. Live,
# the agent held the grant, the server advertised it, and the agent said no such tool existed.
# All fifteen of the platform's own verbs were already underscore-named; this was the first dot.
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z][a-z0-9_]*$")

# The verbs realmtools itself serves. A plugin must not take one: with a dot separator a collision
# was impossible, and with an underscore it is one typo away — and shadowing `run_code` or `rule`
# would be a privilege question rather than an inconvenience.
BUILTIN_VERBS = frozenset({
    "submit_sealed", "reveal_status", "turn_status", "send_private", "run_code", "remember",
    "recall", "reveal", "tally", "score", "penalize", "flag", "scoreboard", "eliminate", "rule",
})

log = logging.getLogger(__name__)


class ToolRisk(StrEnum):
    """How much a grant can cost you if the scenario came from someone else.

    `contained` is metered, chronicled and cannot reach past the platform. `elevated` is anything
    that breaks realm isolation or hands a third party realm content — it takes explicit consent
    at launch (ADR-004 §7). The tool declares its own tier, so a contributed plugin can put itself
    behind that gate without the platform knowing anything about it.
    """

    CONTAINED = "contained"
    ELEVATED = "elevated"


# (args, config, ctx) -> result. `config` is the realm-level `spec.tools[name]` block; `ctx` is
# supplied by the host broker (#54) and carries the verified caller.
ToolHandler = Callable[[dict[str, Any], dict[str, Any], Any], Awaitable[Any]]


@dataclass(frozen=True)
class ToolProfile:
    """Everything the platform needs to offer one tool, and nothing about how it is implemented."""

    name: str
    label: str
    description: str          # the agent reads this; write it for the model, not the operator
    params: dict[str, Any]    # JSON Schema for the call arguments
    handler: ToolHandler
    config_schema: dict[str, Any] = field(default_factory=dict)
    api_key_ref: str | None = None
    risk: ToolRisk = ToolRisk.CONTAINED
    cost_per_call_usd: float = 0.0
    setup_hint: str = ""      # shown when `api_key_ref` has no keystore handle


@runtime_checkable
class ToolPlugin(Protocol):
    def tools(self) -> Iterable[ToolProfile]:
        """The tool profiles this package contributes."""


# Tools that ship with the platform, seeded first so a plugin can never shadow one. `web_fetch`
# needs no key and no third party, so the platform is useful without an install; a search backend
# is a vendor choice with a vendor key and ships as a plugin instead (ADR-004 §2).
BUILTIN_TOOLS: dict[str, ToolProfile] = {}


def _register_builtins() -> None:
    """Imported lazily inside the registry to avoid a cycle: `webfetch` imports ToolProfile."""
    if BUILTIN_TOOLS:
        return
    from bearpit.core.webfetch import WEB_FETCH

    BUILTIN_TOOLS[WEB_FETCH.name] = WEB_FETCH


def _entry_points(group: str) -> list[EntryPoint]:
    """Discovery, isolated so tests can substitute it."""
    return list(entry_points(group=group))


def _load(ep: EntryPoint) -> ToolPlugin | None:
    try:
        obj = ep.load()
    except Exception as exc:  # noqa: BLE001 - a broken plugin must not break the platform
        log.warning("tool plugin %r failed to load: %s", ep.name, exc)
        return None
    # An entry point may resolve to a ready instance, a class, or a factory. A class carries the
    # unbound `tools` attribute, so test for it explicitly rather than by shape.
    if isinstance(obj, type) or (callable(obj) and not hasattr(obj, "tools")):
        try:
            obj = obj()
        except Exception as exc:  # noqa: BLE001
            log.warning("tool plugin %r failed to construct: %s", ep.name, exc)
            return None
    if not callable(getattr(obj, "tools", None)):
        log.warning("tool plugin %r has no tools() — ignored", ep.name)
        return None
    return obj  # type: ignore[no-any-return]


_registry: dict[str, ToolProfile] | None = None


def _accept(into: dict[str, ToolProfile], profile: ToolProfile, origin: str) -> None:
    if not isinstance(profile, ToolProfile):
        log.warning("tool plugin %r contributed a %s, not a ToolProfile — ignored",
                    origin, type(profile).__name__)
        return
    if not TOOL_NAME_RE.match(profile.name):
        log.warning("tool plugin %r contributed an invalid tool name %r — ignored "
                    "(expected 'family_verb', lowercase; a dot never reaches the model)",
                    origin, profile.name)
        return
    if profile.name in BUILTIN_VERBS:
        log.warning("tool plugin %r contributed %r, which is a realmtools verb — ignored",
                    origin, profile.name)
        return
    if profile.name in into:
        log.warning("tool plugin %r contributed %r, which is already provided — ignored "
                    "(the first registration of a name wins)", origin, profile.name)
        return
    into[profile.name] = profile


def tool_registry() -> dict[str, ToolProfile]:
    """Every tool available on this machine: built-ins first, then plugins in installation order.

    Discovered once per process, like provider plugins.
    """
    global _registry
    if _registry is None:
        _register_builtins()
        found: dict[str, ToolProfile] = dict(BUILTIN_TOOLS)
        for ep in _entry_points(TOOL_GROUP):
            plugin = _load(ep)
            if plugin is None:
                continue
            try:
                contributed = list(plugin.tools())
            except Exception as exc:  # noqa: BLE001
                log.warning("tool plugin %r raised in tools(): %s", ep.name, exc)
                continue
            for profile in contributed:
                _accept(found, profile, ep.name)
        _registry = found
    return _registry


def reset_tool_cache() -> None:
    """Forget discovered tools (tests, and any process that installs a plugin at runtime)."""
    global _registry
    _registry = None


def is_tool(name: str) -> bool:
    return name in tool_registry()


MANIFEST_VERSION = 1
MAX_MANIFEST_TOOLS = 64          # a roster cannot plausibly need more; bounds the record
MAX_PARAMS_CHARS = 8000          # one tool's JSON Schema


def grant_manifest(project: Project) -> dict[str, Any]:
    """What agents will be SHOWN for this realm's granted tools (#65).

    Built on the HOST, where the registry lives, and written to the chronicle before any container
    exists. Realmtools then describes every granted tool from this payload alone — which is what
    lets the agent-facing container hold no tool plugins, no third-party dependencies and no
    third-party import-time code.

    A tool that is granted but not installed here is recorded as `available: false` with a reason
    rather than omitted. Omitting it is how a grant becomes invisible with nothing said, and that
    is the failure this whole change exists to remove.
    """
    registry = tool_registry()
    granted = sorted({name for agent in project.agents for name in agent.tools})
    tools: dict[str, Any] = {}
    for name in granted[:MAX_MANIFEST_TOOLS]:
        policy = dict(project.spec.tools.get(name, {}))
        profile = registry.get(name)
        if profile is None:
            tools[name] = {"available": False, "policy": policy,
                           "reason": "not installed on this platform"}
            continue
        params = profile.params
        if len(json.dumps(params, default=str)) > MAX_PARAMS_CHARS:
            log.warning("tool %r has an oversized parameter schema — advertising it unconstrained",
                        name)
            params = {"type": "object"}
        tools[name] = {
            "available": True, "description": profile.description, "params": params,
            "policy": policy, "cost_per_call_usd": profile.cost_per_call_usd,
        }
    if len(granted) > MAX_MANIFEST_TOOLS:
        log.warning("scenario grants %d tools; only the first %d are described",
                    len(granted), MAX_MANIFEST_TOOLS)
    return {
        "version": MANIFEST_VERSION,
        "tools": tools,
        # descriptive only: the signed token decides what an agent may actually call
        "grants": {a.id: list(a.tools) for a in project.agents if a.tools},
    }


def elevated_grants(project: Project) -> dict[str, list[str]]:
    """Granted tools whose tier takes explicit consent, as {agent_id: [tool, ...]} (ADR-004 §7).

    The tier is the TOOL's own declaration, so a contributed plugin puts itself behind the gate
    without the platform knowing anything about it. A grant the platform cannot resolve is not
    reported here — that is a different problem with a different fix, and `check_grants` says so.
    """
    registry = tool_registry()
    out: dict[str, list[str]] = {}
    for agent in project.agents:
        risky = [n for n in agent.tools
                 if (p := registry.get(n)) is not None and p.risk is ToolRisk.ELEVATED]
        if risky:
            out[agent.id] = risky
    return out


def keystore_handles() -> set[str]:
    """Handle names in the local keystore — names only, never a secret value.

    Read here rather than through the Ledger so a caller that only wants to VALIDATE a scenario
    does not have to construct one.
    """
    import json
    from pathlib import Path

    store = Path.home() / ".bearpit" / "keystore.json"
    if not store.exists():
        return set()
    try:
        data = json.loads(store.read_text())
    except (OSError, ValueError):
        return set()
    return set(data) if isinstance(data, dict) else set()


def check_grants(project: Project, *, key_refs: set[str]) -> list[str]:
    """Problems with this project's tool grants **on this machine**, as readable lines.

    Never raises and never mutates: a scenario granting a tool you have not installed is a thing
    to be told about, at the moment it matters, not a file you can no longer open.

    `key_refs` is the set of keystore handles that exist, so a missing key reads as its own
    problem — the fix for "not installed" and the fix for "no key" are different, and reporting
    them as one sends people to the wrong place.
    """
    registry = tool_registry()
    problems: list[str] = []

    for agent in project.agents:
        for name in agent.tools:
            profile = registry.get(name)
            if profile is None:
                problems.append(
                    f"agent {agent.id!r} is granted {name!r}, which is not installed — "
                    f"install the package that provides it, or remove the grant"
                )
                continue
            if profile.api_key_ref and profile.api_key_ref not in key_refs:
                hint = f" ({profile.setup_hint})" if profile.setup_hint else ""
                problems.append(
                    f"agent {agent.id!r} is granted {name!r}, but its key handle "
                    f"{profile.api_key_ref!r} is not in your keystore{hint}"
                )

    for name, config in project.spec.tools.items():
        profile = registry.get(name)
        if profile is None or not profile.config_schema:
            continue  # unknown tools are already reported above, per agent
        problems.extend(_config_problems(name, config, profile.config_schema))

    return problems


def _config_problems(name: str, config: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    return [
        f"spec.tools[{name!r}]{'.' + '.'.join(str(p) for p in e.path) if e.path else ''}: "
        f"{e.message}"
        for e in sorted(validator.iter_errors(config), key=lambda e: list(e.path))
    ]


__all__ = [
    "BUILTIN_TOOLS",
    "BUILTIN_VERBS",
    "TOOL_GROUP",
    "TOOL_NAME_RE",
    "ToolHandler",
    "ToolPlugin",
    "ToolProfile",
    "ToolRisk",
    "MANIFEST_VERSION",
    "check_grants",
    "elevated_grants",
    "grant_manifest",
    "keystore_handles",
    "is_tool",
    "reset_tool_cache",
    "tool_registry",
]
