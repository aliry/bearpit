"""`PackageStore` — the seam between Scribe's authoring tools and scenario packages on disk.

`ApiPackageStore` is the real implementation over the on-disk package layout the loader expects
(`project.json` + `agents/<id>/{agent.json,persona.md}`). It mirrors the Gatekeeper's precedence: a
writable user dir first, then read-only example dirs (a user scenario shadows a bundled example of
the same name). A write serializes the `Project`, re-loads it to validate, then atomically swaps —
so a bad write can never corrupt the existing package. Tests use `FakePackageStore` instead.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Protocol

from agentrealm.core.package import load_package
from agentrealm.core.schema import Project


class PackageStore(Protocol):
    """The authoring tools' view of the world: list/read/write scenario packages."""

    async def list(self) -> list[dict[str, Any]]: ...

    async def read(self, name: str) -> Project: ...

    async def write(self, name: str, project: Project) -> None: ...

    async def has_user(self, name: str) -> bool:
        """Is there a USER-authored scenario of this name? Bundled examples don't count — a write
        may shadow one, same as the editor."""
        ...


def _write_package(root: Path, project: Project) -> None:
    """Serialize a `Project` into the on-disk package layout under `root`.

    Loader-populated fields (`resources`, `resource_files`, `local_skills`, `source`) are omitted;
    `persona` is written to `persona.md`, not `agent.json`. Agents live in `agents/<id>/` folders,
    so `project.json` carries metadata + spec only (an inline-and-folders roster is rejected).
    """
    (root / "agents").mkdir(parents=True, exist_ok=True)
    proj_doc = {
        "apiVersion": project.api_version,
        "kind": project.kind,
        "metadata": project.metadata.model_dump(mode="json", exclude_none=True),
        "spec": project.spec.model_dump(mode="json", by_alias=True, exclude_none=True),
    }
    (root / "project.json").write_text(json.dumps(proj_doc, indent=2))
    for agent in project.agents:
        adir = root / "agents" / agent.id
        adir.mkdir(parents=True, exist_ok=True)
        adoc = agent.model_dump(
            mode="json", by_alias=True, exclude_none=True, exclude={"persona", "resources"}
        )
        adir.joinpath("agent.json").write_text(json.dumps(adoc, indent=2))
        if agent.persona:
            adir.joinpath("persona.md").write_text(agent.persona)


class ApiPackageStore:
    """A `PackageStore` over the on-disk package layout (real impl)."""

    def __init__(self, user_dir: str | Path, example_dirs: list[str | Path] | None = None) -> None:
        self._user_dir = Path(user_dir)
        self._example_dirs = [Path(d) for d in (example_dirs or [])]

    def _bases(self) -> list[Path]:
        return [self._user_dir, *self._example_dirs]

    def _find(self, name: str) -> Path | None:
        for base in self._bases():
            path = base / name
            if (path / "project.json").exists():
                return path
        return None

    async def list(self) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for base in self._bases():
            if not base.is_dir():
                continue
            for d in sorted(base.iterdir()):
                if not (d / "project.json").exists() or d.name in seen:
                    continue
                meta: dict[str, Any] = {}
                try:
                    meta = json.loads((d / "project.json").read_text()).get("metadata", {}) or {}
                except (OSError, json.JSONDecodeError):
                    meta = {}
                agents_dir = d / "agents"
                n_agents = (
                    sum(1 for a in agents_dir.iterdir() if (a / "agent.json").exists())
                    if agents_dir.is_dir()
                    else 0
                )
                seen[d.name] = {
                    "name": d.name,
                    "title": meta.get("name") or d.name,
                    "agents": n_agents,
                    "summary": meta.get("description") or "",
                }
        return sorted(seen.values(), key=lambda p: str(p["name"]))

    async def read(self, name: str) -> Project:
        path = self._find(name)
        if path is None:
            raise FileNotFoundError(f"no scenario {name!r}")
        return load_package(str(path))

    async def has_user(self, name: str) -> bool:
        return (self._user_dir / name / "project.json").exists()

    async def write(self, name: str, project: Project) -> None:
        self._user_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._user_dir / f".{name}.tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        try:
            _write_package(tmp, project)
            load_package(str(tmp))  # validate before committing (never corrupt the live package)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        dest = self._user_dir / name
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
