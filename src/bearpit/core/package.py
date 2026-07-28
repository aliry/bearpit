"""Load and validate a portable project package (architecture §13.5, #38).

A package is a folder:

    my-project/
      project.json            # metadata + spec (JSON canonical; YAML also accepted)
      agents/<id>/agent.json  # one folder per agent = the roster (source of truth)
                  persona.md  # optional; loaded into AgentSpec.persona
                  resources/  # per-agent private resources (discovered)
                  skills/     # per-agent local skills
      resources/  skills/     # project-level shared resources & skills

Secrets never live in the package; credentials are referenced by handle and resolved at
run time. Validation is a hard gate: unique ids, agents/↔roster consistency, local skill
refs resolve, no secret material.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

from bearpit.core.schema import AgentSpec, Project, SkillSource


class PackageError(ValueError):
    """A project package is malformed or fails validation."""


def _load_doc(path: Path) -> dict[str, Any]:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        import yaml  # optional authoring convenience

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise PackageError(f"{path} must contain a JSON/YAML object")
    return data


def _find(root: Path, stem: str) -> Path | None:
    for ext in (".json", ".yaml", ".yml"):
        p = root / f"{stem}{ext}"
        if p.exists():
            return p
    return None


# Fields the platform used to accept and has since REMOVED because nothing ever enforced them
# (see schema.py). `extra="forbid"` is deliberate — a typo must fail loudly — but that same rule
# would brick every scenario an author already has on disk. So a removed field is dropped with a
# warning instead: the manifest still loads, and the author is told the knob does nothing.
_REMOVED_FIELDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("spec", "environment", "roster_visibility"), "it was never enforced; every realm was FULL"),
    (("spec", "environment", "shared_folder", "quota"), "it was never enforced"),
    (("spec", "duration"), "use a `duration` TERMINATION condition, which does work"),
)


def _drop_removed_fields(doc: Any) -> Any:
    """Strip fields the platform no longer has, warning once per field, so an existing manifest
    keeps loading instead of failing validation on a knob that never did anything."""
    if not isinstance(doc, dict):
        return doc
    for path, why in _REMOVED_FIELDS:
        node: Any = doc
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and path[-1] in node:
            node.pop(path[-1])
            warnings.warn(
                f"'{'.'.join(path)}' has been removed from the schema and is ignored — {why}.",
                UserWarning, stacklevel=2,
            )
    return doc


def _contained(path: Path, root: Path) -> bool:
    """True only if `path` really lives inside `root` — following symlinks.

    Packages are portable and shareable (a git repo, a tarball, a plain dir — all preserve
    symlinks), and load_package runs HOST-SIDE. A package that ships
    `agents/x/resources/leak.txt -> /etc/passwd` (or ~/.ssh/id_rsa, a .env) would otherwise be read
    verbatim and seeded INTO the agent's container — and a package configures its own agents'
    egress, so it could then POST the host secret out. Merely loading a package must never read a
    file outside it."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _discover_files(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(
        str(p.relative_to(folder.parent)) for p in folder.rglob("*")
        if p.is_file() and not p.is_symlink() and _contained(p, folder)
    )


def load_package(root: str | Path) -> Project:
    """Load a project into a validated `Project`.

    `root` may be a **package folder** (project.json + agents/<id>/…) or, for the trivial
    flat case, a single **project.json/yaml file** with the roster inline.
    """
    root = Path(root)

    # flat manifest: a single file with agents inline
    if root.is_file():
        project = Project.model_validate(_drop_removed_fields(_load_doc(root)))
        _validate(project, root.parent)
        return project

    if not root.is_dir():
        raise PackageError(f"not a file or directory: {root}")
    proj_file = _find(root, "project")
    if proj_file is None:
        raise PackageError(f"no project.json/yaml in {root}")

    project = Project.model_validate(_drop_removed_fields(_load_doc(proj_file)))

    agents_dir = root / "agents"
    folder_ids = [p.name for p in agents_dir.iterdir() if p.is_dir()] if agents_dir.is_dir() else []
    if project.agents and folder_ids:
        raise PackageError(
            "roster defined both inline in project.json and as agents/ folders — use one"
        )
    if agents_dir.is_dir():
        for agent_dir in sorted(p for p in agents_dir.iterdir() if p.is_dir()):
            agent_file = _find(agent_dir, "agent")
            if agent_file is None:
                raise PackageError(f"agent folder {agent_dir.name} has no agent.json")
            spec = AgentSpec.model_validate(_load_doc(agent_file))
            if spec.id != agent_dir.name:
                raise PackageError(
                    f"agent id {spec.id!r} does not match its folder {agent_dir.name!r}"
                )
            persona_md = agent_dir / "persona.md"
            if persona_md.exists() and not spec.persona:
                spec.persona = persona_md.read_text()
            spec.resources = _discover_files(agent_dir / "resources")
            # ...and actually LOAD them. The names alone were recorded and then dropped on the
            # floor: nothing ever seeded a resource file into a container, so an author could ship
            # a rulebook, a dataset or a brief and their agents would never see one byte of it.
            spec.resource_files = _read_files(agent_dir / "resources")
            spec.local_skills = _read_local_skills(spec, agent_dir)
            project.agents.append(spec)

    project.project_resources = _discover_files(root / "resources")
    shared_res = _read_files(root / "resources")
    for spec in project.agents:
        # project_skills apply to EVERY agent (they were declared and then never used at all)
        for skill in project.project_skills:
            if skill not in spec.skills:
                spec.skills.append(skill)

    # Loaded FILE CONTENTS are excluded from the model dump (they are loader state, not manifest),
    # so the integrity round-trip below would drop them. Stash, re-validate, then re-attach.
    loaded = {
        s.id: (
            {**shared_res, **s.resource_files},  # an agent's own file wins a name clash
            dict(s.local_skills),
        )
        for s in project.agents
    }

    # re-run integrity now that agents are attached
    project = Project.model_validate(project.model_dump(by_alias=True))
    for spec in project.agents:
        res, local = loaded.get(spec.id, ({}, {}))
        spec.resource_files = res
        spec.local_skills = local
    project.source = str(root)  # so a finished run can be relaunched against the CURRENT file
    _validate(project, root)
    return project


def _read_files(folder: Path) -> dict[str, str]:
    """{relative_path: text} for every readable file under `folder` (binaries skipped)."""
    if not folder.is_dir():
        return {}
    out: dict[str, str] = {}
    for f in sorted(folder.rglob("*")):
        # skip symlinks and anything that resolves outside the folder — a shared package must not be
        # able to read host files by shipping a symlink (see _contained).
        if f.is_symlink() or not _contained(f, folder):
            continue
        if not f.is_file() or f.stat().st_size > 1_000_000:
            continue
        try:
            out[str(f.relative_to(folder))] = f.read_text()
        except UnicodeDecodeError:
            continue  # a binary resource can't ride in the prompt/volume as text
    return out


def _read_local_skills(spec: AgentSpec, agent_dir: Path) -> dict[str, str]:
    """{name: SKILL.md} for skills declared with source=local — validated AND loaded.

    They used to be validated (the folder had to exist) and then never delivered: `skill_files`
    only ever handled BUILTIN. So a local skill was checked for existence, and its contents were
    never shown to the agent."""
    out: dict[str, str] = {}
    for skill in spec.skills:
        if skill.source != SkillSource.LOCAL:
            continue
        skills_dir = agent_dir / "skills"
        md = skills_dir / skill.ref / "SKILL.md"
        # skill.ref is author-controlled; a value like "../../../../etc/ssh" would resolve md
        # outside the package and read any host file named SKILL.md. Refuse anything that escapes.
        if not _contained(md, skills_dir) or md.is_symlink():
            raise PackageError(
                f"agent {spec.id!r}: local skill ref {skill.ref!r} escapes the skills directory"
            )
        if not md.exists():
            raise PackageError(
                f"agent {spec.id!r}: local skill {skill.ref!r} needs "
                f"{agent_dir.name}/skills/{skill.ref}/SKILL.md"
            )
        out[skill.ref] = md.read_text()
    return out


def _validate(project: Project, root: Path) -> None:
    if not project.agents:
        raise PackageError("package has no agents (expected agents/<id>/ folders)")
    # (schema already enforced unique ids, single referee, secrets-by-handle.)
    _ = root  # placeholder: credential-handle cross-check lands with the Ledger (M4)
