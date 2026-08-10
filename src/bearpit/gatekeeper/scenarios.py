"""Scenario + skill file I/O for the authoring UI.

The UI edits scenarios as structured data; this module materializes that data back into the
on-disk package layout the loader expects (project.json + agents/<id>/{agent.json,persona.md} +
skills/<ref>/SKILL.md), and handles zip export/import and the custom-skill library. Kept separate
from the HTTP layer so it is unit-testable without FastAPI.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from bearpit.core import PackageError, load_package
from bearpit.forge.skills import BUILTIN_SKILLS

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ScenarioError(ValueError):
    """A scenario/skill write was rejected (bad name, invalid config, missing skill)."""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")


def skills_dir() -> Path:
    """Where user custom skills live (global library the UI + scenarios draw from)."""
    import os

    root = os.environ.get("BEARPIT_SKILLS_DIR")
    return Path(root) if root else Path.home() / ".bearpit" / "skills"


# --- scenarios --------------------------------------------------------------
def _agent_files(agent: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Split an editor agent payload into (agent.json dict, persona.md text)."""
    persona = str(agent.get("persona") or "")
    skills = []
    for s in agent.get("skills") or []:
        src, _, ref = str(s).partition(":")
        if ref:
            skills.append({"source": src, "ref": ref})
    out: dict[str, Any] = {
        "id": agent["id"],
        "name": agent.get("name") or agent["id"],
        "role": agent.get("role") or "participant",
        # the capability tier (small/medium/large); the active provider resolves it at launch
        "model_category": agent.get("model_category") or "medium",
        "budget": agent.get("budget") or {},
        "skills": skills,
    }
    # an optional exact-model override, only written when the editor supplied one
    override = agent.get("model_ref") or agent.get("model")
    if isinstance(override, dict) and override:
        out["model"] = override
    # `tools` belongs here: this list is an allowlist, so a field missing from it is dropped
    # silently on save — the editor showed the grant, the package never carried it (#58).
    for opt in ("rubric", "goals", "responsibilities", "powers", "private_messaging", "color",
                "tools"):
        if agent.get(opt):
            out[opt] = agent[opt]
    return out, persona


def write_scenario(base: Path, name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Write a scenario package from the editor payload, validate it, and return its summary.
    Writes to a temp dir then swaps, so a bad edit never corrupts the existing package."""
    name = _slug(name or data.get("metadata", {}).get("name", ""))
    if not NAME_RE.match(name):
        raise ScenarioError("scenario needs a name (lowercase letters, numbers, dashes)")
    agents = data.get("agents") or []
    if not agents:
        raise ScenarioError("a scenario needs at least one agent")

    tmp = base / f".{name}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "agents").mkdir(parents=True)
    try:
        meta = data.get("metadata")
        meta = dict(meta) if isinstance(meta, dict) else {"name": name}
        meta.setdefault("name", name)
        project = {
            "apiVersion": "bearpit/v1alpha1",
            "kind": "Project",
            "metadata": meta,
            "spec": data.get("spec") or {},
        }
        (tmp / "project.json").write_text(json.dumps(project, indent=2))

        for agent in agents:
            if not agent.get("id"):
                raise ScenarioError("every agent needs an id")
            adir = tmp / "agents" / _slug(agent["id"])
            adir.mkdir(parents=True, exist_ok=True)
            aj, persona = _agent_files(agent)
            adir.joinpath("agent.json").write_text(json.dumps(aj, indent=2))
            adir.joinpath("persona.md").write_text(persona)
            # bundle each custom (local) skill INTO the agent's own skills/ dir (the loader resolves
            # local skills per-agent), so the exported package is self-contained. Copy the WHOLE
            # skill folder — SKILL.md plus any scripts/references/assets it ships with.
            for s in aj["skills"]:
                if s["source"] != "local":
                    continue
                src = _skill_local_dir(s["ref"])
                if not (src / "SKILL.md").is_file():
                    raise ScenarioError(f"custom skill {s['ref']!r} not found in your library")
                shutil.copytree(src, adir / "skills" / s["ref"])

        load_package(str(tmp))  # validate before committing
    except PackageError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ScenarioError(f"invalid scenario: {exc}") from exc
    except ScenarioError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ScenarioError(str(exc)) from exc

    dest = base / name
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return {"name": name, "agents": len(agents)}


def delete_scenario(base: Path, name: str) -> None:
    if not NAME_RE.match(name):
        raise ScenarioError("invalid scenario name")
    dest = base / name
    if not (dest / "project.json").exists():
        raise ScenarioError(f"no editable scenario {name!r}")
    shutil.rmtree(dest)


def export_zip(scenario_path: Path) -> bytes:
    """Zip a scenario folder (folder name as the zip root) into bytes for download."""
    buf = io.BytesIO()
    root = scenario_path.name
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(scenario_path.rglob("*")):
            if f.is_file():
                zf.write(f, f"{root}/{f.relative_to(scenario_path)}")
    return buf.getvalue()


_MAX_ZIP_UNCOMPRESSED = 20_000_000  # zip-bomb guard: cap total extracted bytes, not just the upload


def import_zip(base: Path, zip_bytes: bytes) -> dict[str, Any]:
    """Extract an uploaded scenario zip, validate, and store it. Returns its summary. Hardened
    against zip-slip (path traversal) and zip bombs; the package must sit under one root folder."""
    if len(zip_bytes) > 10_000_000:
        raise ScenarioError("scenario zip too large (>10MB)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise ScenarioError("not a valid .zip file") from exc
    # normalize separators, drop macOS cruft + directory entries; keep real files only
    files = [n for n in zf.namelist()
             if not n.replace("\\", "/").startswith("__MACOSX") and not n.endswith("/")]
    proj = [n for n in files if n.replace("\\", "/").split("/")[-1] == "project.json"]
    if not proj:
        raise ScenarioError("zip has no project.json — not a scenario package")
    # every project.json must share one top-level root dir (reject flat or multi-scenario zips —
    # they are ambiguous, and a differing root is how a crafted zip smuggles files out).
    roots = {n.replace("\\", "/").split("/", 1)[0] for n in proj if "/" in n.replace("\\", "/")}
    if len(roots) != 1:
        raise ScenarioError("zip must contain exactly one scenario folder with a project.json")
    root = roots.pop()
    name = _slug(root)
    if not NAME_RE.match(name):
        raise ScenarioError("invalid scenario folder name in zip")

    tmp = base / f".{name}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    tmp_resolved = tmp.resolve()
    try:
        total = 0
        for n in files:
            norm = n.replace("\\", "/")
            if norm.startswith("/") or ".." in norm.split("/"):
                raise ScenarioError("zip entry escapes the scenario folder")  # zip-slip
            parts = [p for p in norm.split("/") if p and p != "."]
            if len(parts) < 2 or parts[0] != root:
                continue  # outside the single root folder -> ignore
            target = tmp.joinpath(*parts[1:])
            if not target.resolve().is_relative_to(tmp_resolved):  # belt-and-suspenders
                raise ScenarioError("zip entry escapes the scenario folder")
            data = zf.read(n)
            total += len(data)
            if total > _MAX_ZIP_UNCOMPRESSED:
                raise ScenarioError("scenario contents too large when unpacked")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        load_package(str(tmp))
    except ScenarioError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ScenarioError(f"invalid scenario in zip: {exc}") from exc
    dest = base / name
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return {"name": name}


# --- skills ------------------------------------------------------------------
def _frontmatter(content: str, field: str) -> str:
    m = re.search(rf"^{field}:\s*(.+)$", content, re.M)
    return m.group(1).strip() if m else ""


def _set_frontmatter_field(content: str, field: str, value: str) -> str:
    """Add or replace a `field: value` line inside the SKILL.md frontmatter block (creating a
    minimal block if there is none)."""
    if not value:
        return content
    m = re.match(r"(\s*---\n)(.*?)(\n---\s*\n?)(.*)", content, re.S)
    if m:
        head, fm, close, rest = m.groups()
        if re.search(rf"^{field}:\s*.*$", fm, re.M):
            fm = re.sub(rf"^{field}:\s*.*$", f"{field}: {value}", fm, flags=re.M)
        else:
            fm = fm.rstrip("\n") + f"\n{field}: {value}"
        return head + fm + close + rest
    return f"---\n{field}: {value}\n---\n\n{content}"


def _skill_local_dir(ref: str) -> Path:
    """A user skill is a folder (Agent-Skills style): SKILL.md + optional scripts/references."""
    return skills_dir() / _slug(ref)


def list_skills() -> list[dict[str, Any]]:
    """The skill library — one entry per name. Seed skills ship with the platform; a user edit is
    saved as a same-name copy that overrides the seed (merged here, seed hidden). Every skill is
    editable; only user copies can be deleted (deleting reveals the seed again). `files` is the
    total file count (SKILL.md + bundled scripts/references/assets)."""
    merged: dict[str, dict[str, Any]] = {}
    for name, content in BUILTIN_SKILLS.items():
        merged[name] = {"ref": name, "source": "builtin", "editable": True, "deletable": False,
                        "files": 1, "description": _frontmatter(content, "description"),
                        "category": _frontmatter(content, "category")}
    d = skills_dir()
    if d.is_dir():
        for sub in sorted(d.iterdir()):
            md = sub / "SKILL.md"
            if md.is_file():
                content = md.read_text()
                merged[sub.name] = {"ref": sub.name, "source": "local", "editable": True,
                                    "deletable": True,
                                    "files": sum(1 for f in sub.rglob("*") if f.is_file()),
                                    "description": _frontmatter(content, "description"),
                                    "category": _frontmatter(content, "category")}
    return sorted(merged.values(), key=lambda s: s["ref"])


def skill_content(source: str, ref: str) -> str | None:
    if source == "builtin":
        return BUILTIN_SKILLS.get(ref)
    if source == "local":
        return custom_skill_content(ref)
    return None


def custom_skill_content(ref: str) -> str | None:
    md = _skill_local_dir(ref) / "SKILL.md"
    return md.read_text() if md.is_file() else None


def skill_tree(source: str, ref: str) -> list[str]:
    """Relative paths of every file in a skill (SKILL.md first). Built-ins are SKILL.md only."""
    if source == "builtin":
        return ["SKILL.md"] if ref in BUILTIN_SKILLS else []
    d = _skill_local_dir(ref)
    if not (d / "SKILL.md").is_file():
        return []
    files = [str(f.relative_to(d)).replace("\\", "/") for f in d.rglob("*") if f.is_file()]
    return sorted(files, key=lambda p: (p != "SKILL.md", p))  # SKILL.md pinned to the top


def read_skill_file(source: str, ref: str, relpath: str) -> str | None:
    """Text content of one file inside a skill; None if missing or binary (path-traversal safe)."""
    relpath = relpath.replace("\\", "/")
    if source == "builtin":
        return BUILTIN_SKILLS.get(ref) if relpath == "SKILL.md" else None
    if source != "local":
        return None
    d = _skill_local_dir(ref).resolve()
    parts = [p for p in relpath.split("/") if p and p != "."]
    if ".." in parts or not parts:
        return None
    target = d.joinpath(*parts)
    if not target.resolve().is_relative_to(d) or not target.is_file():
        return None
    try:
        return target.read_text()
    except (UnicodeDecodeError, OSError):
        return None  # binary asset — not viewable as text


def export_skill_zip(source: str, ref: str) -> bytes:
    """Zip a whole skill folder (root = <ref>/) for download — SKILL.md plus any bundled files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if source == "builtin":
            content = BUILTIN_SKILLS.get(ref)
            if content is None:
                raise ScenarioError(f"no skill {ref!r}")
            zf.writestr(f"{_slug(ref)}/SKILL.md", content)
        else:
            d = _skill_local_dir(ref)
            if not (d / "SKILL.md").is_file():
                raise ScenarioError(f"no skill {ref!r}")
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    zf.write(f, f"{d.name}/{f.relative_to(d)}")
    return buf.getvalue()


def write_custom_skill(name: str, content: str, category: str | None = None) -> dict[str, Any]:
    name = _slug(name)
    if not NAME_RE.match(name):
        raise ScenarioError("skill needs a name (lowercase letters, numbers, dashes)")
    if not content.strip():
        raise ScenarioError("skill content is empty")
    if not content.lstrip().startswith("---"):  # give it valid frontmatter if the user omitted it
        desc = content.strip().splitlines()[0][:58] if content.strip() else name
        content = f"---\nname: {name}\ndescription: {desc}\nversion: 1.0.0\n---\n\n{content}"
    if category is not None:  # the Category field is the source of truth for the frontmatter tag
        content = _set_frontmatter_field(content, "category", category.strip())
    d = skills_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("SKILL.md").write_text(content)
    return {"ref": name, "source": "local", "description": _frontmatter(content, "description"),
            "category": _frontmatter(content, "category")}


def import_skill_file(filename: str, content: str) -> dict[str, Any]:
    """Import an uploaded SKILL.md into the library. The name comes from the frontmatter `name:`
    field, else the file's own name (minus a bare 'SKILL')."""
    if len(content) > 200_000:
        raise ScenarioError("skill file too large")
    if not content.strip():
        raise ScenarioError("the file is empty")
    stem = Path(filename or "").stem
    if stem.lower() == "skill":
        stem = ""  # a bare 'SKILL.md' carries no name; fall through to a default
    ref = _slug(_frontmatter(content, "name") or stem or "imported-skill")
    return write_custom_skill(ref, content)


_MAX_SKILL_TOTAL = 5_000_000  # total unpacked bytes for a multi-file skill (bomb guard)


def _store_skill_tree(name: str, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Write a whole skill folder (root-relative paths) into the library, atomically and safely."""
    name = _slug(name)
    if not NAME_RE.match(name):
        raise ScenarioError("skill needs a name (lowercase letters, numbers, dashes)")
    if not any(rel == "SKILL.md" for rel, _ in files):
        raise ScenarioError("a skill must include a SKILL.md at its root")
    tmp = skills_dir() / f".{name}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    resolved = tmp.resolve()
    try:
        total = 0
        for rel, data in files:
            parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
            if not parts or ".." in parts or rel.startswith("/"):
                raise ScenarioError("a skill file escapes the skill folder")
            target = tmp.joinpath(*parts)
            if not target.resolve().is_relative_to(resolved):
                raise ScenarioError("a skill file escapes the skill folder")
            total += len(data)
            if total > _MAX_SKILL_TOTAL:
                raise ScenarioError("skill contents too large when unpacked")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except ScenarioError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    dest = _skill_local_dir(name)
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    content = (dest / "SKILL.md").read_text(errors="replace")
    return {"ref": name, "source": "local", "files": len(files),
            "description": _frontmatter(content, "description"),
            "category": _frontmatter(content, "category")}


def _import_skill_from_entries(entries: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Store a skill from raw (path, bytes) entries (from a zip or a folder upload). Finds the
    shallowest SKILL.md, strips its root folder, and names the skill from that folder (or the
    frontmatter `name:`)."""
    clean = [(p.replace("\\", "/"), d) for p, d in entries
             if not p.replace("\\", "/").startswith("__MACOSX")
             and not p.rsplit("/", 1)[-1].startswith("._")]
    mds = [p for p, _ in clean if p.rsplit("/", 1)[-1] == "SKILL.md"]
    if not mds:
        raise ScenarioError("no SKILL.md found — not an Agent Skill")
    md = min(mds, key=lambda p: p.count("/"))
    root = md.split("/")[0] if "/" in md else ""
    prefix = root + "/" if root else ""
    tree: list[tuple[str, bytes]] = []
    md_content = ""
    for p, d in clean:
        if prefix and not p.startswith(prefix):
            continue  # a sibling file outside the skill's root folder
        rel = p[len(prefix):]
        if not rel:
            continue
        tree.append((rel, d))
        if rel == "SKILL.md":
            md_content = d.decode("utf-8", errors="replace")
    name = _slug(root or _frontmatter(md_content, "name") or "imported-skill")
    return _store_skill_tree(name, tree)


def import_skill_zip(zip_bytes: bytes) -> dict[str, Any]:
    """Import an Agent Skill packaged as a .zip (SKILL.md + optional scripts/references/assets)."""
    if len(zip_bytes) > _MAX_SKILL_TOTAL:
        raise ScenarioError("skill zip too large (>5MB)")
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise ScenarioError("not a valid .zip file") from exc
    entries = [(n, zf.read(n)) for n in zf.namelist() if not n.endswith("/")]
    return _import_skill_from_entries(entries)


def import_skill_folder(files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Import an Agent Skill from an uploaded folder (list of (relative-path, bytes))."""
    return _import_skill_from_entries(files)


def delete_custom_skill(name: str) -> None:
    d = skills_dir() / _slug(name)
    if not (d / "SKILL.md").is_file():
        raise ScenarioError(f"no custom skill {name!r}")
    shutil.rmtree(d)


# Only GitHub's raw-content hosts are fetchable — an allowlist, not a blocklist, so a user-supplied
# URL can never be pointed at an internal service or cloud-metadata endpoint (SSRF).
_RAW_HOSTS = ("raw.githubusercontent.com", "gist.githubusercontent.com")


def _raw_github_url(url: str) -> str:
    """Turn a github.com blob URL (or a raw/gh:// ref) into a raw.githubusercontent.com URL."""
    url = url.strip()
    m = re.match(r"gh://([^/]+)/([^@/]+)(?:@([^/]+))?/(.+)", url)
    if m:
        org, repo, ref, path = m.groups()
        return f"https://raw.githubusercontent.com/{org}/{repo}/{ref or 'main'}/{path}"
    if "github.com" in url and "/blob/" in url:
        return url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url  # assume it is already a raw URL (validated against the host allowlist below)


async def import_gh_skill(url: str, name: str | None = None) -> dict[str, Any]:
    """Fetch a SKILL.md from a GitHub repo/URL into the custom library. The fetch is pinned to
    GitHub's raw hosts (no redirects, no arbitrary hosts) so a crafted URL can't reach internal
    services; the content is stored as data, never executed."""
    from urllib.parse import urlparse

    import httpx

    raw = _raw_github_url(url)
    host = urlparse(raw).netloc.lower()
    if urlparse(raw).scheme != "https" or host not in _RAW_HOSTS:
        raise ScenarioError(
            "provide a github.com/<org>/<repo>/blob/<ref>/<path> or gh://<org>/<repo>/<path> URL"
        )
    try:
        # follow_redirects=False: a redirect off the pinned host would defeat the allowlist.
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
            r = await c.get(raw)
            r.raise_for_status()
            content = r.text
    except Exception as exc:
        raise ScenarioError(f"could not fetch skill: {exc}") from exc
    if len(content) > 200_000:
        raise ScenarioError("skill file too large")
    ref = _slug(name or _frontmatter(content, "name") or raw.rsplit("/", 2)[-2])
    return write_custom_skill(ref, content)
