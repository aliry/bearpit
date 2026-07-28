"""Small persisted platform state (not per-realm, not secret): the active model provider AND the
editable provider→category→model tables.

Backed by `~/.bearpit/platform.json` so pipeline settings survive restarts and are set over the
API without editing env or any scenario manifest. Reads tolerate a missing/corrupt file by falling
back to the seeded defaults.
"""

from __future__ import annotations

import copy
import json
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


def active_provider() -> str:
    """The active model-provider name, or the default when unset/unknown."""
    name = _read().get("model_provider")
    if isinstance(name, str) and is_provider(name, providers_config()):
        return name
    return DEFAULT_PROVIDER


def set_active_provider(name: str) -> str:
    """Persist the active model-provider name. Raises ValueError for an unknown provider."""
    if not is_provider(name, providers_config()):
        raise ValueError(f"unknown model provider {name!r}")
    state = _read()
    state["model_provider"] = name
    _write(state)
    return name
