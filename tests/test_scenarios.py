"""Scenario + skill file I/O for the authoring UI: write/validate, delete, zip round-trip, and the
custom-skill library. Uses tmp dirs (via env) so nothing touches the real ~/.bearpit."""
import io
import json
import zipfile

import pytest

from bearpit.gatekeeper import scenarios as sc


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("BEARPIT_SKILLS_DIR", str(tmp_path / "skills"))
    return tmp_path


def _payload(name="my-game", **spec):
    return {
        "metadata": {"name": name, "description": "a test game"},
        "spec": {"termination": [{"type": "manual"}], **spec},
        "agents": [
            {"id": "host", "role": "referee", "persona": "# Host\nYou judge.",
             "model_category": "large", "skills": ["builtin:referee-social-deduction"]},
            {"id": "vela", "role": "participant", "persona": "# Vela",
             "model_category": "small", "skills": []},
        ],
    }


def test_write_scenario_materializes_package(dirs):
    out = sc.write_scenario(dirs, "my-game", _payload())
    assert out == {"name": "my-game", "agents": 2}
    root = dirs / "my-game"
    assert json.loads((root / "project.json").read_text())["metadata"]["name"] == "my-game"
    aj = json.loads((root / "agents" / "host" / "agent.json").read_text())
    assert aj["role"] == "referee" and aj["skills"] == [{"source": "builtin", "ref":
                                                         "referee-social-deduction"}]
    assert (root / "agents" / "host" / "persona.md").read_text().startswith("# Host")


def test_write_slugifies_and_requires_agents(dirs):
    out = sc.write_scenario(dirs, "My Cool Game!", _payload("My Cool Game!"))
    assert out["name"] == "my-cool-game"
    with pytest.raises(sc.ScenarioError, match="at least one agent"):
        sc.write_scenario(dirs, "empty", {"metadata": {"name": "empty"}, "agents": []})


def test_write_rejects_invalid_and_keeps_old(dirs):
    sc.write_scenario(dirs, "keep", _payload("keep"))
    bad = _payload("keep")
    bad["agents"][0]["model_category"] = "gigantic"  # invalid category -> load_package fails
    with pytest.raises(sc.ScenarioError):
        sc.write_scenario(dirs, "keep", bad)
    # the previously-valid package is untouched, and no temp dir is left behind
    assert (dirs / "keep" / "project.json").exists()
    assert not list(dirs.glob(".keep.tmp"))


def test_write_bundles_used_custom_skills(dirs):
    sc.write_custom_skill("my-move", "You may bluff.")  # in the global library
    p = _payload("bundled")
    p["agents"][1]["skills"] = ["local:my-move"]
    sc.write_scenario(dirs, "bundled", p)
    # the local skill is copied INTO the agent's own skills/ dir (loader resolves per-agent)
    md = dirs / "bundled" / "agents" / "vela" / "skills" / "my-move" / "SKILL.md"
    assert md.read_text().find("bluff") > 0


def test_delete_scenario(dirs):
    sc.write_scenario(dirs, "gone", _payload("gone"))
    sc.delete_scenario(dirs, "gone")
    assert not (dirs / "gone").exists()
    with pytest.raises(sc.ScenarioError, match="no editable"):
        sc.delete_scenario(dirs, "gone")


def test_export_then_import_roundtrips(dirs):
    sc.write_scenario(dirs, "trip", _payload("trip"))
    blob = sc.export_zip(dirs / "trip")
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert "trip/project.json" in names and "trip/agents/host/agent.json" in names
    # importing the same bytes into a fresh base reproduces the package
    dest = dirs / "reimport"
    dest.mkdir()
    out = sc.import_zip(dest, blob)
    assert out["name"] == "trip" and (dest / "trip" / "project.json").exists()


def test_import_zip_rejects_non_scenario(dirs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("notes/readme.txt", "hi")
    with pytest.raises(sc.ScenarioError, match="no project.json"):
        sc.import_zip(dirs, buf.getvalue())


def test_import_zip_rejects_slip_and_multiroot(dirs):
    proj = json.dumps({"metadata": {"name": "ok"}, "spec": {"termination": [{"type": "manual"}]}})
    # path-traversal entry must be refused (zip-slip)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ok/project.json", proj)
        zf.writestr("ok/../../evil.txt", "pwned")
    with pytest.raises(sc.ScenarioError, match="escape"):
        sc.import_zip(dirs / "a", buf.getvalue())
    # two scenario roots is ambiguous -> refused
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as zf:
        zf.writestr("one/project.json", proj)
        zf.writestr("two/project.json", proj)
    with pytest.raises(sc.ScenarioError, match="exactly one"):
        sc.import_zip(dirs / "b", buf2.getvalue())


async def test_import_gh_skill_rejects_non_github_hosts(dirs):
    # SSRF guard: only GitHub raw hosts are fetchable; internal/metadata URLs are refused up front
    for bad in ("http://169.254.169.254/latest/meta-data/",
                "https://internal.local/skill.md",
                "http://localhost:8000/api/settings"):
        with pytest.raises(sc.ScenarioError, match="github"):
            await sc.import_gh_skill(bad)


def test_custom_skill_crud_and_frontmatter(dirs):
    out = sc.write_custom_skill("bluffing", "Bluff convincingly and never break character.")
    assert out["ref"] == "bluffing" and out["source"] == "local"
    content = sc.custom_skill_content("bluffing")
    assert content.startswith("---") and "description:" in content  # frontmatter auto-added
    listed = sc.list_skills()
    # every skill is editable; only user copies are deletable (a seed skill is not)
    assert any(s["ref"] == "bluffing" and s["editable"] and s["deletable"] for s in listed)
    seed = next(s for s in listed if s["ref"] == "referee-social-deduction")
    assert seed["editable"] and not seed["deletable"] and seed["source"] == "builtin"
    sc.delete_custom_skill("bluffing")
    assert sc.custom_skill_content("bluffing") is None


def test_editing_a_seed_skill_overrides_it_by_name(dirs):
    # saving a custom skill with a seed's name merges to ONE entry (the user copy), now deletable;
    # deleting it reveals the seed again.
    ref = "referee-social-deduction"
    sc.write_custom_skill(ref, "My custom gamemaster rules.", category="Refereeing")
    entries = [s for s in sc.list_skills() if s["ref"] == ref]
    assert len(entries) == 1 and entries[0]["source"] == "local" and entries[0]["deletable"]
    assert entries[0]["category"] == "Refereeing"
    sc.delete_custom_skill(ref)
    seed = next(s for s in sc.list_skills() if s["ref"] == ref)
    assert seed["source"] == "builtin" and not seed["deletable"]  # seed restored


def _skill_zip(root="deep-research"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{root}/SKILL.md", "---\nname: deep-research\ndescription: research well\n"
                    "category: Research\n---\nUse the scripts.")
        zf.writestr(f"{root}/scripts/search.py", "print('searching')\n")
        zf.writestr(f"{root}/references/method.md", "# Method\nCite sources.")
    return buf.getvalue()


def test_import_skill_zip_keeps_all_files(dirs):
    out = sc.import_skill_zip(_skill_zip())
    assert out["ref"] == "deep-research" and out["files"] == 3 and out["category"] == "Research"
    tree = sc.skill_tree("local", "deep-research")
    assert tree == ["SKILL.md", "references/method.md", "scripts/search.py"]  # SKILL.md first
    assert "searching" in sc.read_skill_file("local", "deep-research", "scripts/search.py")
    assert sc.read_skill_file("local", "deep-research", "../secret") is None  # traversal blocked


def test_export_skill_zip_roundtrips_a_folder(dirs):
    sc.import_skill_zip(_skill_zip())
    blob = sc.export_skill_zip("local", "deep-research")
    names = set(zipfile.ZipFile(io.BytesIO(blob)).namelist())
    assert {"deep-research/SKILL.md", "deep-research/scripts/search.py",
            "deep-research/references/method.md"} <= names


def test_import_skill_folder_upload(dirs):
    files = [("my-skill/SKILL.md", b"---\nname: my-skill\ndescription: x\n---\nbody"),
             ("my-skill/scripts/run.sh", b"echo hi\n")]
    out = sc.import_skill_folder(files)
    assert out["ref"] == "my-skill" and out["files"] == 2
    assert sc.skill_tree("local", "my-skill") == ["SKILL.md", "scripts/run.sh"]


def test_import_skill_zip_rejects_slip_and_no_skillmd(dirs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ok/SKILL.md", "---\nname: ok\n---\nx")
        zf.writestr("ok/../evil.txt", "pwn")
    with pytest.raises(sc.ScenarioError, match="escape"):
        sc.import_skill_zip(buf.getvalue())
    nofile = io.BytesIO()
    with zipfile.ZipFile(nofile, "w") as zf:
        zf.writestr("x/notes.md", "hi")
    with pytest.raises(sc.ScenarioError, match="no SKILL.md"):
        sc.import_skill_zip(nofile.getvalue())


def test_folder_skill_bundles_into_scenario(dirs):
    sc.import_skill_zip(_skill_zip())  # a 3-file skill in the library
    p = _payload("uses-skill")
    p["agents"][1]["skills"] = ["local:deep-research"]
    sc.write_scenario(dirs, "uses-skill", p)
    # the WHOLE skill folder is copied into the agent's package, not just SKILL.md
    base = dirs / "uses-skill" / "agents" / "vela" / "skills" / "deep-research"
    assert (base / "SKILL.md").is_file() and (base / "scripts" / "search.py").is_file()


def test_write_custom_skill_preserves_existing_frontmatter(dirs):
    body = "---\nname: pre\ndescription: keep me\nversion: 2.0.0\n---\n\nBody here."
    sc.write_custom_skill("pre", body)
    assert sc.custom_skill_content("pre") == body  # not re-wrapped


def test_raw_github_url_forms():
    assert sc._raw_github_url("gh://org/repo/skills/a/SKILL.md") == (
        "https://raw.githubusercontent.com/org/repo/main/skills/a/SKILL.md")
    assert sc._raw_github_url("gh://org/repo@v2/a.md") == (
        "https://raw.githubusercontent.com/org/repo/v2/a.md")
    assert sc._raw_github_url("https://github.com/org/repo/blob/main/x/SKILL.md") == (
        "https://raw.githubusercontent.com/org/repo/main/x/SKILL.md")
    assert sc._raw_github_url("https://raw.githubusercontent.com/o/r/main/a.md") == (
        "https://raw.githubusercontent.com/o/r/main/a.md")
