"""Load a real project-package folder end to end."""

import json
from pathlib import Path

import pytest

from agentrealm.core import AgentRole, PackageError, load_package


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def _pkg(root: Path) -> None:
    _write(root / "project.json", {
        "apiVersion": "agentrealm/v1alpha1",
        "kind": "Project",
        "metadata": {"name": "rps-duel", "author": "example-author"},
        "spec": {
            "environment": {"network_egress": "model_only"},
            "termination": [{"type": "message", "channel": "commons", "pattern": "MATCH OVER"}],
            "mechanics": [{"kind": "sealed-submit", "ruleset": "dominance"}],
        },
    })
    for aid, role in (("vela", "participant"), ("orin", "participant"), ("themis", "referee")):
        _write(root / "agents" / aid / "agent.json", {
            "id": aid,
            "role": role,
            "model": {"provider": "azure", "model": "gpt-5.4-mini", "api_key_ref": "azure-main",
                      "input_cost_per_token": 1e-7, "output_cost_per_token": 6e-7},
            "budget": {"max_usd": 1.0, "on_exhausted": "starve_then_kill", "grace_period": "10m"},
            "skills": [{"source": "builtin", "ref": "agent-basics"}],
            **({"rubric": "rock beats scissors"} if role == "referee" else {}),
        })
    (root / "agents" / "vela" / "persona.md").write_text("# Vela\nYou play to win.")
    (root / "agents" / "vela" / "resources").mkdir(parents=True)
    (root / "agents" / "vela" / "resources" / "brief.txt").write_text("your brief")


def test_load_full_package(tmp_path: Path):
    _pkg(tmp_path)
    proj = load_package(tmp_path)
    assert proj.metadata.name == "rps-duel"
    assert {a.id for a in proj.agents} == {"vela", "orin", "themis"}
    assert proj.referee is not None and proj.referee.id == "themis"
    vela = next(a for a in proj.agents if a.id == "vela")
    assert vela.persona and "play to win" in vela.persona  # loaded from persona.md
    assert "resources/brief.txt" in vela.resources  # discovered, relative to agent folder
    assert proj.spec.mechanics[0].ruleset == "dominance"


def test_agent_folder_id_mismatch(tmp_path: Path):
    _pkg(tmp_path)
    # rename a folder so its name no longer matches the agent.json id
    (tmp_path / "agents" / "vela").rename(tmp_path / "agents" / "velaX")
    with pytest.raises(PackageError):
        load_package(tmp_path)


def test_missing_project_file(tmp_path: Path):
    (tmp_path / "agents").mkdir()
    with pytest.raises(PackageError):
        load_package(tmp_path)


def test_local_skill_must_exist(tmp_path: Path):
    _pkg(tmp_path)
    aj = tmp_path / "agents" / "orin" / "agent.json"
    data = json.loads(aj.read_text())
    data["skills"] = [{"source": "local", "ref": "made-up-skill"}]
    aj.write_text(json.dumps(data))
    with pytest.raises(PackageError):
        load_package(tmp_path)


def test_referee_role_detected(tmp_path: Path):
    _pkg(tmp_path)
    proj = load_package(tmp_path)
    assert proj.referee is not None
    assert proj.referee.role == AgentRole.REFEREE
    assert proj.referee.powers is not None


def test_resources_and_local_skills_are_actually_LOADED_not_just_listed(tmp_path):
    """`resources` recorded FILE NAMES and threw the contents away; nothing ever seeded one into a
    container. `SkillSource.LOCAL` was validated (the folder had to exist) and then never delivered
    — skill_files() only handled BUILTIN. So an author could ship a rulebook and a hand-written
    SKILL.md, have both accepted, and their agents would never see a byte of either."""
    from agentrealm.core.package import load_package

    root = tmp_path / "pkg"
    (root / "agents" / "vela" / "resources").mkdir(parents=True)
    (root / "agents" / "vela" / "skills" / "house-style").mkdir(parents=True)
    (root / "resources").mkdir()

    (root / "project.json").write_text(json.dumps({
        "apiVersion": "agentrealm/v1alpha1", "kind": "Project",
        "metadata": {"name": "p"},
        "spec": {"goals": ["g"]},
    }))
    (root / "agents" / "vela" / "agent.json").write_text(json.dumps({
        "id": "vela", "model_category": "small",
        "skills": [{"source": "local", "ref": "house-style"}],
    }))
    res = root / "agents" / "vela" / "resources"
    res.joinpath("rulebook.md").write_text("# The rules\nNo bluffing.")
    sk = root / "agents" / "vela" / "skills" / "house-style"
    sk.joinpath("SKILL.md").write_text("# House style\nBe terse.")
    (root / "resources" / "shared-brief.md").write_text("# Brief\nShip it.")

    p = load_package(root)
    vela = p.agents[0]
    # the NAMES are still listed (that is what the author sees)...
    assert vela.resources == ["resources/rulebook.md"]
    # ...and now the CONTENTS are actually loaded — the agent's own file AND the project's
    assert vela.resource_files["rulebook.md"] == "# The rules\nNo bluffing."
    assert vela.resource_files["shared-brief.md"] == "# Brief\nShip it."
    # the local skill's text is loaded too, not merely checked for existence
    assert vela.local_skills["house-style"] == "# House style\nBe terse."


def test_a_manifest_using_a_removed_field_still_loads_with_a_warning(tmp_path):
    """`extra="forbid"` is deliberate — a typo must fail loudly. But applying it to a field we
    REMOVED would brick every scenario an author already has saved on disk. So a removed field is
    dropped with a warning: the manifest loads, and the author is told the knob does nothing."""
    import warnings

    from agentrealm.core.package import load_package

    root = tmp_path / "pkg"
    root.mkdir()
    (root / "project.json").write_text(json.dumps({
        "apiVersion": "agentrealm/v1alpha1", "kind": "Project",
        "metadata": {"name": "p"},
        "spec": {
            "goals": ["g"],
            "duration": "1h",                                    # removed
            "environment": {"roster_visibility": "full",         # removed
                            "shared_folder": {"enabled": True, "quota": "2GiB"}},  # removed
        },
        "agents": [{"id": "a", "model_category": "small"}],
    }))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        p = load_package(root)
    said = " ".join(str(w.message) for w in caught)
    assert "roster_visibility" in said and "duration" in said and "quota" in said
    assert p.spec.environment.shared_folder.enabled is True   # the real setting survives


def test_a_symlinked_resource_pointing_outside_the_package_is_refused(tmp_path):
    """Packages are portable and shareable, and load_package runs HOST-SIDE. A package that ships
    `agents/x/resources/leak.txt -> /etc/passwd` would otherwise be read verbatim and seeded into
    the agent's container — and the package configures its own agents' egress, so it could POST the
    host secret out. Merely loading a package must never read a file outside it."""
    import os

    from agentrealm.core.package import load_package

    root = tmp_path / "pkg"
    (root / "agents" / "spy" / "resources").mkdir(parents=True)
    secret = tmp_path / "host-secret.txt"
    secret.write_text("SUPER SECRET HOST FILE")
    (root / "project.json").write_text(json.dumps({
        "apiVersion": "agentrealm/v1alpha1", "kind": "Project",
        "metadata": {"name": "p"}, "spec": {"goals": ["g"]},
    }))
    (root / "agents" / "spy" / "agent.json").write_text(json.dumps(
        {"id": "spy", "model_category": "small"}))
    os.symlink(secret, root / "agents" / "spy" / "resources" / "leak.txt")

    p = load_package(root)   # loads fine — it simply does NOT read through the symlink
    assert "SUPER SECRET HOST FILE" not in json.dumps(p.agents[0].resource_files)
    assert p.agents[0].resource_files == {}


def test_a_local_skill_ref_that_escapes_the_skills_dir_is_rejected(tmp_path):
    from agentrealm.core.package import PackageError, load_package

    root = tmp_path / "pkg"
    (root / "agents" / "x").mkdir(parents=True)
    (root / "project.json").write_text(json.dumps({
        "apiVersion": "agentrealm/v1alpha1", "kind": "Project",
        "metadata": {"name": "p"}, "spec": {"goals": ["g"]},
    }))
    (root / "agents" / "x" / "agent.json").write_text(json.dumps({
        "id": "x", "model_category": "small",
        "skills": [{"source": "local", "ref": "../../../../etc/ssh"}],
    }))
    with pytest.raises(PackageError, match="escapes the skills directory"):
        load_package(root)
