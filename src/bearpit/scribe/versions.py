"""Version history (§8.6) — what makes direct-apply safe.

Every write snapshots the PRIOR package state first, so any create/edit is reviewable and
revertible. Snapshots live in a `<root>/<name>/<ts>-<hash>.json` tree (no git dependency). The
timestamp comes from an injected `clock` so ids are deterministic under test. `diff_projects` is the
human-readable, field-level diff shown before a direct-apply (and by `preview_changes`).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bearpit.core.schema import Project
from bearpit.scribe.store import PackageStore

_MISSING = object()


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_flatten(v, f"{prefix}.{i}"))
    else:
        out[prefix] = obj
    return out


def diff_projects(before: Project | None, after: Project) -> str:
    """A human-readable field-level diff: `+` added, `-` removed, `~` changed (before -> after)."""
    b_flat = _flatten(before.model_dump(by_alias=True, exclude_none=True)) if before else {}
    a_flat = _flatten(after.model_dump(by_alias=True, exclude_none=True))
    lines: list[str] = []
    for key in sorted(set(b_flat) | set(a_flat)):
        b = b_flat.get(key, _MISSING)
        a = a_flat.get(key, _MISSING)
        if b is _MISSING:
            lines.append(f"+ {key}: {a!r}")
        elif a is _MISSING:
            lines.append(f"- {key}: {b!r}")
        elif b != a:
            lines.append(f"~ {key}: {b!r} -> {a!r}")
    return "\n".join(lines) if lines else "(no changes)"


class Versions:
    """A `<root>/<name>/` tree of JSON snapshots of prior package state."""

    def __init__(self, root: str | Path, clock: Callable[[], float] = time.time) -> None:
        self._root = Path(root)
        self._clock = clock

    def _dir(self, name: str) -> Path:
        return self._root / name

    async def snapshot(self, name: str, project: Project | None) -> str:
        """Record the prior state (`None` = pre-create) and return the version id."""
        manifest = (
            project.model_dump(mode="json", by_alias=True, exclude_none=True)
            if project is not None
            else None
        )
        ts_ms = int(self._clock() * 1000)
        digest = hashlib.sha1(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:8]
        version_id = f"{ts_ms:015d}-{digest}"
        d = self._dir(name)
        d.mkdir(parents=True, exist_ok=True)
        doc = {"id": version_id, "created": ts_ms, "project": manifest}
        (d / f"{version_id}.json").write_text(json.dumps(doc, indent=2))
        return version_id

    async def list(self, name: str) -> list[dict[str, Any]]:
        """Snapshots for `name`, newest first: `[{id, created, pre_create}]`."""
        d = self._dir(name)
        if not d.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for f in sorted(d.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append(
                {
                    "id": data.get("id", f.stem),
                    "created": data.get("created"),
                    "pre_create": data.get("project") is None,
                }
            )
        return out

    async def revert(self, name: str, version_id: str, store: PackageStore) -> None:
        """Restore `name` to the snapshot `version_id` by writing it back through the store."""
        path = self._dir(name) / f"{version_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"no version {version_id!r} for scenario {name!r}")
        data = json.loads(path.read_text())
        manifest = data.get("project")
        if manifest is None:
            raise ValueError(
                f"version {version_id!r} is a pre-create snapshot — there is no prior state to "
                "restore (delete the scenario instead)."
            )
        await store.write(name, Project.model_validate(manifest))
