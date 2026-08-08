"""Small persisted platform state (not per-realm, not secret): the active model provider AND the
editable provider→category→model tables.

Backed by `~/.bearpit/platform.json` so pipeline settings survive restarts and are set over the
API without editing env or any scenario manifest. Reads tolerate a missing/corrupt file by falling
back to the seeded defaults.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bearpit.core.providers import (
    CATEGORIES,
    DEFAULT_PROVIDER,
    default_providers,
    is_provider,
)


def _path() -> Path:
    d = Path.home() / ".bearpit"
    d.mkdir(parents=True, exist_ok=True)
    return d / "platform.json"


def _read() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _write(state: dict[str, Any]) -> None:
    _path().write_text(json.dumps(state, indent=2))


def providers_config() -> dict[str, dict[str, Any]]:
    """The provider tables: seeded defaults deep-merged with any user overrides from disk."""
    cfg = default_providers()
    stored = _read().get("providers")
    if isinstance(stored, dict):
        for name, prof in stored.items():
            if not isinstance(prof, dict):
                continue
            base = cfg.setdefault(name, {"categories": {}})
            for k, v in prof.items():
                if k == "categories" and isinstance(v, dict):
                    cats = base.setdefault("categories", {})
                    for cat, entry in v.items():
                        if isinstance(entry, dict):
                            cats[cat] = {**cats.get(cat, {}), **entry}
                else:
                    base[k] = v
    return cfg


def set_providers_config(providers: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Persist edited provider tables (stored whole; merged with defaults on read). Validates that
    each provider has the three categories with a non-empty model name."""
    if not isinstance(providers, dict) or not providers:
        raise ValueError("providers must be a non-empty object")
    for name, prof in providers.items():
        cats = (prof or {}).get("categories") if isinstance(prof, dict) else None
        if not isinstance(cats, dict):
            raise ValueError(f"provider {name!r} needs a 'categories' object")
        for cat in CATEGORIES:
            entry = cats.get(cat)
            if not isinstance(entry, dict) or not str(entry.get("model") or "").strip():
                raise ValueError(f"provider {name!r} category {cat!r} needs a model name")
    state = _read()
    state["providers"] = copy.deepcopy(providers)
    _write(state)
    return providers_config()


_log = logging.getLogger(__name__)
_warned: set[str] = set()


@dataclass(frozen=True)
class ProviderChoice:
    """Which provider a run will actually use, and whether that is what was asked for."""

    name: str  # what will be used
    stored: str | None  # what platform.json asked for, if anything
    available: bool  # whether `stored` is a provider this install can resolve
    reason: str = ""  # why it fell back, phrased for a human; "" when it did not

    @property
    def fell_back(self) -> bool:
        """A provider was chosen and is NOT the one being used.

        Distinct from "never configured": only a substitution is worth interrupting anyone about."""
        return self.stored is not None and not self.available


def resolve_provider(*, warn: bool = True) -> ProviderChoice:
    """The effective provider plus why, so a substitution can be surfaced instead of swallowed.

    A provider contributed by a plugin stops being resolvable the moment that plugin is missing —
    `uv sync` prunes anything outside the lockfile, including one installed with `uv pip install
    -e`. The stored setting is still on disk and still correct; it simply cannot be honoured. Left
    silent, the operator launches what they believe is a flat-rate run and is billed
    for a metered one (#47)."""
    stored = _read().get("model_provider")
    stored = stored if isinstance(stored, str) and stored else None
    if stored is None:
        return ProviderChoice(name=DEFAULT_PROVIDER, stored=None, available=True)
    if is_provider(stored, providers_config()):
        return ProviderChoice(name=stored, stored=stored, available=True)
    # A clause, not a sentence: every caller composes it into its own phrasing (log line, API
    # detail, Settings alert, CLI warning). A self-contained sentence read as a duplicate
    # everywhere it was embedded — "X is configured, but X is configured but…".
    reason = "its provider plugin is not installed"
    # `warn=False` for a caller that presents this itself — the CLI printed the log line and its
    # own styled prompt back to back, which reads like the same problem happening twice.
    if warn and stored not in _warned:  # once per provider per process; settings polling is chatty
        _warned.add(stored)
        _log.warning(
            "model provider %r is configured but %s — runs will use %r instead",
            stored, reason, DEFAULT_PROVIDER,
        )
    return ProviderChoice(
        name=DEFAULT_PROVIDER, stored=stored, available=False, reason=reason
    )


def active_provider() -> str:
    """The active model-provider name, or the default when unset/unknown.

    Unchanged contract. Use `resolve_provider()` where a caller can act on the difference."""
    return resolve_provider().name


def set_active_provider(name: str) -> str:
    """Persist the active model-provider name. Raises ValueError for an unknown provider."""
    if not is_provider(name, providers_config()):
        raise ValueError(f"unknown model provider {name!r}")
    state = _read()
    state["model_provider"] = name
    _write(state)
    _warned.discard(name)  # it is resolvable again; a later disappearance should warn afresh
    return name
