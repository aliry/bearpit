"""Gatekeeper API: read endpoints (against SQLite) + control endpoints (fake manager)."""
import json

import pytest
from fakes import FLAT
from fakes import flat_rate_table as _flat_rate_table
from starlette.testclient import TestClient

from bearpit.chronicle import Chronicle, EventKind
from bearpit.gatekeeper.api import create_app, realm_status
from bearpit.gatekeeper.manager import CapacityError


class FakeManager:
    def __init__(self, max_active=6):
        self.runs = {}
        self.started = []
        self.stopped = []
        self.projects = {}
        self.parameters = {}
        self.max_active = max_active

    def start(self, realm_id, project, *, require_mention=True, parameters=None,
              allow_provider_fallback=False):
        if len(self.active()) >= self.max_active:
            raise CapacityError(f"{self.max_active} realms already running")
        self.started.append((realm_id, len(project.agents)))
        self.projects[realm_id] = project
        self.parameters = dict(parameters or {})
        self.allow_provider_fallback = allow_provider_fallback

    def stop(self, realm_id):
        self.stopped.append(realm_id)

    def active(self):
        return [r for r, _ in self.started]


@pytest.fixture
async def seeded():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await chron.append_event("duel", EventKind.LIFECYCLE, {"event": "running"})
    await chron.append_event("duel", EventKind.SPEND, {"agent": "vela", "usd": 0.02})
    await chron.append_event("duel", EventKind.SCORE, {"agent": "orin", "delta": 3})
    await chron.append_event("duel", EventKind.VIOLATION, {"agent": "vela", "reason": "late"})
    await chron.append_event("duel", EventKind.VERDICT, {"outcome": "orin wins"})
    await chron.record_message("duel", "!c", "@duel-vela", "hi")
    yield chron
    await chron.close()


async def test_realm_status_aggregates(seeded):
    s = await realm_status(seeded, "duel")
    assert s["state"] == "running" and s["outcome"] == "orin wins"
    # verdict here carries no scoreboard -> fall back to the raw SCORE-event ledger
    assert s["scores"] == {"orin": 3.0} and s["spend"] == {"vela": 0.02}
    assert s["score_discrepancy"] is False
    assert s["violations"] == [{"agent": "vela", "reason": "late"}]


async def test_ruled_scoreboard_is_authoritative_over_a_corrupted_ledger():
    # rps-rv1 in miniature: a duplicate SCORE write inflates the raw ledger to orin 3 / vela 2,
    # but the referee RULED on its own board of orin 2 / vela 2 (a genuine 2-2 draw). Once ruled,
    # status must surface the verdict board (2-2), keep the raw ledger for transparency, and FLAG
    # the divergence — never silently show the corrupted 3-2 that contradicts the outcome string.
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        await chron.append_event("m", EventKind.LIFECYCLE, {"event": "running"})
        for agent, reason in [("orin", "R1"), ("orin", "R1"), ("orin", "R3"),  # dup R1 = corruption
                              ("vela", "R7"), ("vela", "R8")]:
            await chron.append_event("m", EventKind.SCORE,
                                     {"agent": agent, "delta": 1, "reason": reason})
        await chron.append_event("m", EventKind.VERDICT, {
            "outcome": "draw — match tied 2-2", "final": True,
            "scoreboard": {"orin": 2.0, "vela": 2.0}})
        s = await realm_status(chron, "m")
        assert s["scores"] == {"orin": 2.0, "vela": 2.0}          # the ruling, not the re-sum
        assert s["score_ledger"] == {"orin": 3.0, "vela": 2.0}    # raw log kept for transparency
        assert s["score_discrepancy"] is True                     # divergence is surfaced
    finally:
        await chron.close()


def test_read_endpoints(seeded):
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        assert "Bearpit" in c.get("/").text  # dashboard
        realms = c.get("/api/realms").json()["realms"]
        assert any(r["realm_id"] == "duel" for r in realms)
        st = c.get("/api/realms/duel").json()
        assert st["outcome"] == "orin wins" and st["scores"] == {"orin": 3.0}
        assert c.get("/api/realms/nope").status_code == 404
        tr = c.get("/api/realms/duel/transcript").json()["messages"]
        assert tr[-1]["sender"] == "@duel-vela"
        # the events timeline (debugging: floor grants, verdicts, spend) — filterable by kind
        evs = c.get("/api/realms/duel/events").json()["events"]
        assert {e["kind"] for e in evs} >= {"lifecycle", "spend", "verdict"}
        verdicts = c.get("/api/realms/duel/events", params={"kind": "verdict"}).json()["events"]
        assert [e["payload"]["outcome"] for e in verdicts] == ["orin wins"]
        assert "orin wins" in c.get("/api/realms/duel/report").text


async def test_status_reports_failed(seeded):
    await seeded.append_event("boom", EventKind.LIFECYCLE, {"event": "provisioning"})
    await seeded.append_event("boom", EventKind.LIFECYCLE, {"event": "failed", "detail": "x"})
    assert (await realm_status(seeded, "boom"))["state"] == "failed"


def test_orphaned_running_realm_reads_interrupted(seeded):
    # a realm the chronicle last saw "running" but that no live task owns = interrupted
    app = create_app(chron=seeded, manager=FakeManager())  # duel not in the manager
    with TestClient(app) as c:
        st = c.get("/api/realms/duel").json()
        assert st["active"] is False and st["state"] == "interrupted"


async def test_reconcile_orphans_marks_interrupted(seeded):
    from bearpit.gatekeeper.api import reconcile_orphans

    await seeded.append_event("live", EventKind.LIFECYCLE, {"event": "running"})
    n = await reconcile_orphans(seeded)
    assert n >= 1
    assert (await realm_status(seeded, "live"))["state"] == "interrupted"


def test_capacity_returns_429(seeded, tmp_path):
    import json

    (tmp_path / "project.json").write_text(json.dumps({
        "metadata": {"name": "duel"}, "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "v", "model": {"provider": "azure", "model": "m",
                                         "api_key_ref": "azure-main"}}],
    }))
    app = create_app(chron=seeded, manager=FakeManager(max_active=0))  # already full
    with TestClient(app) as c:
        r = c.post("/api/realms", json={"package": str(tmp_path)})
        assert r.status_code == 429 and "already running" in r.json()["detail"]


def test_packages_endpoint_lists_examples(seeded):
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        names = {p["name"] for p in c.get("/api/packages").json()["packages"]}
        assert {"rps-duel", "pitch-contest"} <= names  # scanned from examples/


def test_import_scenario_folder(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path / "imports"))
    proj = json.dumps({"metadata": {"name": "My Duel"},
                       "spec": {"termination": [{"type": "manual"}]}})
    agent = json.dumps({"id": "vela",
                        "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}})
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        files = [
            ("files", ("my-duel/project.json", proj, "application/json")),
            ("files", ("my-duel/agents/vela/agent.json", agent, "application/json")),
            ("files", ("my-duel/agents/vela/persona.md", "# Vela", "text/markdown")),
        ]
        r = c.post("/api/packages/import", files=files)
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "my-duel" and r.json()["agents"] == 1
        # it now appears in the list + has a detail page
        assert "my-duel" in {p["name"] for p in c.get("/api/packages").json()["packages"]}
        d = c.get("/api/packages/my-duel").json()
        assert d["title"] == "My Duel" and any(a["id"] == "vela" for a in d["agents"])
        # a folder with no project.json is rejected + cleaned up
        bad = c.post("/api/packages/import", files=[("files", ("x/notes.txt", "hi", "text/plain"))])
        assert bad.status_code == 400


def test_create_with_turns_override(seeded, tmp_path):
    (tmp_path / "project.json").write_text(json.dumps({
        "metadata": {"name": "deb"}, "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "a", "model": {"provider": "azure", "model": "m",
                                         "api_key_ref": "azure-main"}}],
    }))
    mgr = FakeManager()
    app = create_app(chron=seeded, manager=mgr)
    with TestClient(app) as c:
        c.post("/api/realms", json={"package": str(tmp_path), "realm_id": "r1",
                                    "turns": {"enabled": True, "silence_timeout_s": 45}})
        assert mgr.projects["r1"].spec.turns.silence_timeout_s == 45  # override applied
        c.post("/api/realms", json={"package": str(tmp_path), "realm_id": "r2",
                                    "turns": {"enabled": False}})
        assert mgr.projects["r2"].spec.turns is None  # explicitly disabled


def test_package_detail_for_preview(seeded):
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        d = c.get("/api/packages/rps-duel").json()
        assert d["title"] == "rps-duel" and d["referee"] == "themis"
        # the preview exposes the turns block (None when the scenario is a free-for-all). rps-duel
        # now runs turns so the referee gets a round boundary to resolve on — assert the SHAPE, not
        # a scenario detail that is allowed to change.
        assert "turns" in d
        assert d["turns"] is None or d["turns"]["policy"] == "one-at-a-time"
        assert {a["id"] for a in d["agents"]} >= {"vela", "orin", "themis"}
        themis = next(a for a in d["agents"] if a["id"] == "themis")
        assert themis["role"] == "referee" and themis["rubric"]  # roster carries instructions
        assert d["mechanics"] and d["mechanics"][0]["ruleset"] == "dominance"
        assert c.get("/api/packages/does-not-exist").status_code == 404
        assert c.get("/api/packages/bad%20name").status_code in (400, 404)


def test_create_and_stop(seeded, tmp_path):
    import json

    (tmp_path / "project.json").write_text(json.dumps({
        "metadata": {"name": "duel"},
        "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "vela",
                    "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}}],
    }))
    mgr = FakeManager()
    app = create_app(chron=seeded, manager=mgr)
    with TestClient(app) as c:
        r = c.post("/api/realms", json={"package": str(tmp_path), "realm_id": "duel2"})
        assert r.status_code == 200 and r.json()["realm_id"] == "duel2"
        assert mgr.started == [("duel2", 1)]
        assert c.post("/api/realms/duel2/stop").json()["state"] == "stopping"
        assert mgr.stopped == ["duel2"]
        # bad package -> 400
        assert c.post("/api/realms", json={"package": "/nope"}).status_code == 400


def _participant_effects_package(tmp_path):
    """A machine whose PLAYERS may write the game's own data — the opt-in the spec says is
    surfaced at launch the way elevated tool grants are (§2)."""
    (tmp_path / "project.json").write_text(json.dumps({
        "metadata": {"name": "self-dealt"},
        "spec": {
            "termination": [{"type": "manual"}],
            "mechanics": [{"kind": "state-machine", "machine": {
                "roles": {"ref": {"members": "referee"},
                          "player": {"members": "participants"}},
                "states": ["a", "b"], "initial": "a",
                "participant_effects": ["set"],
                "data": {"pot": {"visibility": "public"}},
                "transitions": {"go": {"from": "a", "to": "b", "by": "player",
                                       "effects": [{"set": {"key": "pot",
                                                            "value": "$args.n"}}]}},
            }}],
        },
        "agents": [
            {"id": "ref", "role": "referee", "rubric": "score them",
             "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}},
            {"id": "vela",
             "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}},
        ],
    }))
    return tmp_path


def test_participant_effects_take_consent_at_launch(seeded, tmp_path):
    """`participant_effects` is the user choosing LAW over physics for their game: the players
    may now rewrite the machine's own data, which is a refereeless self-dealt table and a
    legitimate experiment — but it is a choice, and one nobody makes by accident. It goes through
    the same door as an elevated tool grant (ADR-004 §7): refused once, naming what it is, and
    launched on the same `allow_elevated_tools` consent."""
    app = create_app(chron=seeded, manager=FakeManager())
    pkg = str(_participant_effects_package(tmp_path))
    with TestClient(app) as c:
        blocked = c.post("/api/realms", json={"package": pkg})
        allowed = c.post("/api/realms", json={"package": pkg, "allow_elevated_tools": True})
    assert 400 <= blocked.status_code < 500, blocked.text
    detail = blocked.json()["detail"]
    assert "participant_effects" in json.dumps(detail)
    assert detail["machine_participant_effects"] == ["set"]
    assert "allow_elevated_tools" in detail["hint"]
    assert allowed.status_code == 200, allowed.text


def test_a_machine_without_participant_effects_launches_silently(seeded, tmp_path):
    """The consent only means something if the ordinary machine realm never sees it (#47)."""
    from test_core_schema import _machine_spec

    (tmp_path / "project.json").write_text(json.dumps({
        "metadata": {"name": "refereed"},
        "spec": {"termination": [{"type": "manual"}], "mechanics": [_machine_spec()]},
        "agents": [
            {"id": "ref", "role": "referee", "rubric": "score them",
             "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}},
            {"id": "vela",
             "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}},
        ],
    }))
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        r = c.post("/api/realms", json={"package": str(tmp_path)})
    assert r.status_code == 200, r.text


def _editor_payload(name="ui-game"):
    return {
        "metadata": {"name": name, "description": "made in the UI"},
        "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "vela", "role": "participant",
                    "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}}],
    }


def test_scenario_crud_via_api(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path / "scen"))
    monkeypatch.setenv("BEARPIT_EXAMPLES_DIR", str(tmp_path / "examples"))  # isolate from repo
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        # create
        r = c.post("/api/packages", json=_editor_payload())
        assert r.status_code == 200 and r.json() == {"name": "ui-game", "agents": 1}
        assert "ui-game" in {p["name"] for p in c.get("/api/packages").json()["packages"]}
        # read back for the editor
        d = c.get("/api/packages/ui-game").json()
        assert d["title"] == "ui-game" and d["agents"][0]["id"] == "vela"
        # update (add an agent)
        payload = _editor_payload()
        payload["agents"].append({"id": "orin", "role": "participant",
                                  "model": {"provider": "azure", "model": "m",
                                            "api_key_ref": "azure-main"}})
        assert c.put("/api/packages/ui-game", json=payload).json()["agents"] == 2
        # export -> zip, then delete
        z = c.get("/api/packages/ui-game/export")
        assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
        assert z.content[:2] == b"PK"
        assert c.delete("/api/packages/ui-game").json() == {"deleted": "ui-game"}
        assert "ui-game" not in {p["name"] for p in c.get("/api/packages").json()["packages"]}
        # invalid create -> 400
        assert c.post("/api/packages", json={"metadata": {"name": "x"}, "agents": []}).status_code \
            == 400


def test_zip_import_via_api(seeded, tmp_path, monkeypatch):
    import io
    import zipfile

    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path / "scen"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("zebra/project.json", json.dumps({"metadata": {"name": "Zebra"},
                    "spec": {"termination": [{"type": "manual"}]}}))
        zf.writestr("zebra/agents/vela/agent.json", json.dumps({"id": "vela",
                    "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}}))
        zf.writestr("zebra/agents/vela/persona.md", "# Vela")
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        r = c.post("/api/packages/import-zip",
                   files={"file": ("zebra.zip", buf.getvalue(), "application/zip")})
        assert r.status_code == 200 and r.json()["name"] == "zebra"
        assert "zebra" in {p["name"] for p in c.get("/api/packages").json()["packages"]}


def test_a_scenarios_local_skills_round_trip_through_the_editor(seeded, tmp_path, monkeypatch):
    """A local skill is a SKILL.md the AGENT carries, so its text is scenario state. The editor has
    to be able to read it, change it, and save it back — into that agent, in that scenario. The
    library copy (and every other scenario that attached the same ref) stays as it was."""
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path / "scen"))
    monkeypatch.setenv("BEARPIT_SKILLS_DIR", str(tmp_path / "skills"))  # deliberately EMPTY
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        d = c.get("/api/packages/poker-table").json()
        vega = next(a for a in d["agents"] if a["id"] == "vega")
        # the editor gets the text, per agent, not just the ref
        assert "pot-odds" in vega["local_skills"]
        assert "Pricing a hand" in vega["local_skills"]["pot-odds"]
        assert "table-notes" in next(a for a in d["agents"] if a["id"] == "mira")["local_skills"]

        body = _editor_body(d)
        edited = next(a for a in body["agents"] if a["id"] == "vega")
        edited["local_skills"]["pot-odds"] += "\n\nAlways price the river.\n"
        assert c.put("/api/packages/poker-table", json=body).status_code == 200

        after = c.get("/api/packages/poker-table").json()
        again = next(a for a in after["agents"] if a["id"] == "vega")
        assert again["local_skills"]["pot-odds"].endswith("Always price the river.\n")
        assert "Pricing a hand" in again["local_skills"]["pot-odds"], "an edit, not a replacement"
        # the edit is this agent's alone: rigel attaches the same ref and did not change
        rigel = next(a for a in after["agents"] if a["id"] == "rigel")
        assert "Always price the river." not in rigel["local_skills"]["pot-odds"]
        # ...and the library (empty here) was never written to
        assert not (tmp_path / "skills").exists()
        # the bundled example is a read-only template; the edit went to the user dir
        assert (tmp_path / "scen" / "poker-table" / "agents" / "vega" / "skills" / "pot-odds"
                / "SKILL.md").is_file()


def _editor_body(d):
    """The detail payload as the scenario editor hands it back on save (see detailToState/save in
    app.js): skills as "source:ref" strings, and each agent's own local skill text."""
    return {
        "metadata": {"name": d["title"], "description": d["description"], "tags": d["tags"],
                     "author": d["author"], "category": d["category"]},
        "spec": {"goals": d["goals"], "guidelines": d["guidelines"],
                 "restrictions": d["restrictions"], "parameters": d["parameters"],
                 "termination": d["termination"], "mechanics": d["mechanics"],
                 "turns": d["turns"], "referee_opens": d["referee_opens"],
                 "provide_tools": d["provide_tools"], "stall_nudge": d["stall_nudge"],
                 "environment": {**d["environment"],
                                 "shared_folder": {"enabled": d["environment"]["shared_folder"]}}},
        "agents": [{"id": a["id"], "name": a["name"], "role": a["role"],
                    "model_category": a["model_category"], "budget": a["budget_ref"],
                    "private_messaging": a["private_messaging"], "persona": a["persona"],
                    "rubric": a["rubric"], "goals": a["goals"], "skills": a["skills"],
                    "tools": a["tools"], "local_skills": a["local_skills"]}
                   for a in d["agents"]],
    }


def test_skills_api(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("BEARPIT_SKILLS_DIR", str(tmp_path / "skills"))
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        skills = c.get("/api/skills").json()["skills"]
        assert any(s["ref"] == "social-deduction" and s["source"] == "builtin" for s in skills)
        # built-in content is fetchable
        content = c.get("/api/skills/builtin/social-deduction").json()["content"]
        assert "hidden-role" in content
        # create a custom skill, it appears + is fetchable + deletable
        assert c.post("/api/skills", json={"name": "bluff", "content": "Bluff well."}).json()[
            "ref"] == "bluff"
        assert "bluff" in {s["ref"] for s in c.get("/api/skills").json()["skills"]}
        assert "Bluff well" in c.get("/api/skills/local/bluff").json()["content"]
        assert c.delete("/api/skills/bluff").json() == {"deleted": "bluff"}
        assert c.get("/api/skills/local/bluff").status_code == 404


def test_runs_and_settings_endpoints(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path / "scen"))
    monkeypatch.setenv("BEARPIT_SKILLS_DIR", str(tmp_path / "skills"))
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        runs = c.get("/api/runs").json()["runs"]
        duel = next(r for r in runs if r["realm_id"] == "duel")
        assert duel["state"] == "archived" or duel["state"] == "interrupted"  # not live in FakeMgr
        assert duel["outcome"] == "orin wins" and duel["scenario"] == "duel"
        s = c.get("/api/settings").json()
        assert s["capacity"] == 6 and "skills_builtin" in s and isinstance(s["api_key_refs"], list)


def test_serialize_project_puts_referee_first():
    from bearpit.core.schema import AgentRole, AgentSpec, ModelRef, Project, ProjectMeta
    from bearpit.gatekeeper.api import serialize_project

    def agent(aid, role=AgentRole.PARTICIPANT):
        return AgentSpec(id=aid, role=role,
                         model=ModelRef(provider="azure", model="m", api_key_ref="k"))
    # referee declared LAST in the roster
    project = Project(metadata=ProjectMeta(name="g"),
                      agents=[agent("p1"), agent("p2"), agent("host", AgentRole.REFEREE)])
    out = serialize_project(project, "g", "examples/g")
    assert out["referee"] == "host"
    assert out["agents"][0]["id"] == "host"  # referee heads the roster
    assert [a["id"] for a in out["agents"]] == ["host", "p1", "p2"]  # others keep their order


def test_serialize_project_includes_skill_contents():
    from bearpit.core.schema import AgentSpec, ModelRef, Project, ProjectMeta, SkillRef
    from bearpit.gatekeeper.api import serialize_project

    agent = AgentSpec(id="p1", model=ModelRef(provider="azure", model="m", api_key_ref="k"),
                      skills=[SkillRef(source="builtin", ref="social-deduction")])
    proj = Project(metadata=ProjectMeta(name="g"), agents=[agent])
    out = serialize_project(proj, "g", "examples/g")
    # the built-in skill's full SKILL.md text is available for the UI to show on click
    assert "builtin:social-deduction" in out["skill_contents"]
    assert "hidden-role" in out["skill_contents"]["builtin:social-deduction"]


def test_run_config_snapshots_what_actually_ran_not_what_the_manifest_asked_for():
    """The manifest is not what runs. Between launch and the first token the platform rewrites the
    project three times: resolve_project picks the concrete model + effort from the tier,
    pace_turns_for_provider raises silence_timeout to a slow pipeline's floor, and
    raise_budgets_for_flat_rate_provider lifts a too-tight cap on a fixed-price one. A scenario
    asking for `large / 120s / $2` may well run as `fake-l::high / 240s / $25`. Rendering the
    manifest on the realm page would show a configuration that never existed."""
    from bearpit.core.providers import (
        pace_turns_for_provider,
        raise_budgets_for_flat_rate_provider,
        resolve_project,
    )
    from bearpit.core.runconfig import run_config
    from bearpit.core.schema import Project

    project = Project.model_validate({
        "apiVersion": "bearpit/v1alpha1", "kind": "Project",
        "metadata": {"name": "p"},
        "spec": {
            "goals": ["g"],
            "turns": {"silence_timeout_s": 120},          # will be RAISED to the CLI floor
            "termination": [{"type": "duration", "limit": "30m"}],
        },
        "agents": [
            {"id": "mother", "role": "referee", "model_category": "large",
             "rubric": "judge", "budget": {"max_usd": 2.0}},       # will be RAISED
            {"id": "cass", "model_category": "small",
             "private_messaging": {"enabled": True, "peers": ["mother"], "max_per_round": 2}},
        ],
    })
    asked_timeout = project.spec.turns.silence_timeout_s
    asked_budget = project.agents[0].budget.max_usd

    table = _flat_rate_table()
    project = resolve_project(project, FLAT, table)
    project = pace_turns_for_provider(project, FLAT, table)
    project = raise_budgets_for_flat_rate_provider(project, FLAT, table)
    cfg = run_config(project, FLAT, require_mention=True)

    # the RESOLVED values, which is the whole point
    assert cfg["turns"]["silence_timeout_s"] > asked_timeout      # raised to the pipeline floor
    mother = next(a for a in cfg["agents"] if a["id"] == "mother")
    assert mother["budget_usd"] > asked_budget                    # raised off the flat-rate floor
    assert mother["model"] and mother["effort"]                   # a concrete model, not a tier
    assert mother["model_category"] == "large"                    # ...and what it ASKED for
    assert mother["role"] == "referee"

    # the questions people actually ask of a finished run
    assert cfg["free_response"] is False and cfg["require_mention"] is True
    assert cfg["provider"] == FLAT
    assert cfg["referee"] == "mother"
    assert any(t["type"] == "duration" for t in cfg["termination"])
    cass = next(a for a in cfg["agents"] if a["id"] == "cass")
    assert cass["private_messaging"] == {
        "enabled": True, "peers": ["mother"], "include_referee": False, "max_per_round": 2,
    }
    # the skills it ACTUALLY carried: Forge always seeds the role core on top of the declared list,
    # so reporting only the manifest's list would under-report what the agent was really told.
    assert "referee-basics" in mother["skills"]
    assert "agent-basics" in cass["skills"]


def test_free_for_all_realm_reports_no_turns_and_a_seeing_referee():
    from bearpit.core.providers import AZURE
    from bearpit.core.runconfig import run_config
    from bearpit.core.schema import Project

    project = Project.model_validate({
        "apiVersion": "bearpit/v1alpha1", "kind": "Project",
        "metadata": {"name": "p"},
        "spec": {"goals": ["g"]},   # no turns block at all
        "agents": [{"id": "judge", "role": "referee", "rubric": "score", "model_category": "large"},
                   {"id": "pro", "model_category": "small"}],
    })
    cfg = run_config(project, AZURE, require_mention=True)
    assert cfg["turns"] is None                 # free-for-all
    assert cfg["referee_sees_all"] is True      # the judge is exempt from the mention gate here


def test_a_machine_realms_referee_is_never_in_the_firehose():
    """`referee_reads_commons` no longer lifts the mention gate, and this pins that it cannot.

    The flag used to mean both "may read the floor" and "is woken by every word on it". Buying the
    second to get the first put the dealer at ~50% of all messages and ~90% of spend in two live
    runs, narrating its own inaction. It now grants `table_talk` — a pull — and a machine realm's
    referee stays gated whichever way the flag is set."""
    from test_core_schema import _machine_spec, _project

    from bearpit.core.runconfig import run_config

    for reads in (False, True):
        p = _project({"mechanics": [_machine_spec(referee_reads_commons=reads)]})
        cfg = run_config(p, provider="x", require_mention=True)
        assert cfg["referee_sees_all"] is False, f"referee_reads_commons={reads} lifted the gate"


def test_rerun_snapshot_replays_the_run_and_ignores_later_edits(seeded):
    """Running it again is TWO different things, and conflating them is how you "reproduce" a bug
    against code that no longer has it. `snapshot` restores the RESOLVED project captured at launch
    — same models, budgets, personas — even if the scenario file has been edited since or the active
    provider has been switched."""
    from bearpit.core.providers import resolve_project
    from bearpit.core.schema import Project
    from bearpit.gatekeeper.runner import _project_snapshot, project_from_snapshot

    project = Project.model_validate({
        "apiVersion": "bearpit/v1alpha1", "kind": "Project",
        "metadata": {"name": "duel"},
        "spec": {"goals": ["g"]},
        "agents": [{"id": "vela", "model_category": "small", "persona": "# Vela\nThe ORIGINAL."}],
    })
    project = resolve_project(project, FLAT, _flat_rate_table())
    project.agents[0].resource_files = {"rules.md": "# rules"}   # loader state (exclude=True)
    project.agents[0].local_skills = {"house": "# house style"}

    snap = _project_snapshot(project)
    restored = project_from_snapshot(dict(snap))

    # the exact model it ran on, not a tier to be re-resolved against whatever is active now
    assert restored.agents[0].model.model == project.agents[0].model.model
    assert restored.agents[0].model.effort == project.agents[0].model.effort
    assert restored.agents[0].persona == "# Vela\nThe ORIGINAL."
    # loader state is exclude=True, so a naive model_dump would have SILENTLY dropped the reference
    # files and the hand-written skill the original agents were given
    assert restored.agents[0].resource_files == {"rules.md": "# rules"}
    assert restored.agents[0].local_skills == {"house": "# house style"}


def test_rerun_rejects_a_realm_with_no_captured_configuration(seeded):
    # realms that ran before configurations were captured cannot be replayed — say so plainly
    # instead of launching something subtly different and calling it the same run.
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        r = c.post("/api/realms/duel-001/rerun?mode=snapshot")
        assert r.status_code == 409
        assert "launch the scenario" in r.json()["detail"]
        assert c.post("/api/realms/duel-001/rerun?mode=sideways").status_code == 400


# ------------------------------------------------------------------ parameters (ADR-003, #41)

def _param_scenario(root, name="param-demo", parameters=None):
    """A package on disk whose prose carries placeholders."""
    d = root / name
    (d / "agents" / "orin").mkdir(parents=True)
    (d / "project.json").write_text(json.dumps({
        "metadata": {"name": name, "description": "a ${category,fruit} relay"},
        "spec": {
            "goals": ["reach ${target,10,Points to win}", "for ${team_name,,Who is playing}"],
            "termination": [{"type": "manual"}],
            **({"parameters": parameters} if parameters else {}),
        },
    }))
    (d / "agents" / "orin" / "agent.json").write_text(json.dumps({
        "id": "orin", "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}}))
    (d / "agents" / "orin" / "persona.md").write_text("You play for ${team_name}")
    return d


def test_the_parameters_endpoint_describes_the_launch_form(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path))
    _param_scenario(tmp_path)
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        r = c.get("/api/packages/param-demo/parameters")
        assert r.status_code == 200, r.text
        by_name = {p["name"]: p for p in r.json()["parameters"]}
        assert set(by_name) == {"category", "target", "team_name"}
        assert by_name["target"]["default"] == "10"
        assert by_name["target"]["description"] == "Points to win"
        assert by_name["target"]["required"] is False
        assert by_name["team_name"]["required"] is True
        assert "agents.orin.persona" in by_name["team_name"]["used_in"], (
            "the form shows where each parameter is used, which is what makes a typo visible"
        )


def test_the_endpoint_reports_a_manifest_override(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path))
    _param_scenario(tmp_path, parameters={"target": {"default": "99"}})
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        p = {x["name"]: x for x in
             c.get("/api/packages/param-demo/parameters").json()["parameters"]}["target"]
        assert (p["default"], p["default_origin"], p["inline_default"]) == ("99", "manifest", "10")
        assert p["overridden"] is True


def test_launching_without_a_required_parameter_is_a_400_that_says_which(
    seeded, tmp_path, monkeypatch
):
    """The UI renders this list; a bare 400 would leave the operator guessing."""
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path))
    pkg = _param_scenario(tmp_path)
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        r = c.post("/api/realms", json={"package": str(pkg)})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert [m["name"] for m in detail["missing"]] == ["team_name"]
        assert detail["missing"][0]["description"] == "Who is playing"
        assert "allow_missing_parameters" in detail["hint"]


def test_explicit_consent_lets_an_empty_parameter_through(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path))
    pkg = _param_scenario(tmp_path)
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        r = c.post("/api/realms",
                   json={"package": str(pkg), "allow_missing_parameters": True})
        assert r.status_code == 200, r.text


def test_supplied_values_reach_the_started_project(seeded, tmp_path, monkeypatch):
    """The whole point: the bound project is what runs, and it is what gets snapshotted."""
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path))
    pkg = _param_scenario(tmp_path)
    manager = FakeManager()
    app = create_app(chron=seeded, manager=manager)
    with TestClient(app) as c:
        r = c.post("/api/realms", json={
            "package": str(pkg),
            "parameters": {"team_name": "Blue Pair", "target": "25", "category": "colour"},
        })
        assert r.status_code == 200, r.text
    realm_id = manager.started[-1][0]
    started = manager.projects[realm_id]
    assert started.spec.goals == ["reach 25", "for Blue Pair"]
    assert started.metadata.description == "a colour relay"
    assert started.agents[0].persona == "You play for Blue Pair", (
        "persona lives in a package FILE, so this also proves the loader-populated text is bound"
    )


def test_a_bad_value_is_rejected_before_anything_starts(seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path))
    pkg = _param_scenario(tmp_path, parameters={"category": {"choices": ["fruit", "colour"]}})
    manager = FakeManager()
    app = create_app(chron=seeded, manager=manager)
    with TestClient(app) as c:
        r = c.post("/api/realms", json={
            "package": str(pkg),
            "parameters": {"team_name": "X", "category": "furniture"}})
        assert r.status_code == 400
        assert "must be one of" in r.json()["detail"]
    assert manager.started == [], "nothing may be provisioned after a rejected value"


def test_editing_a_scenario_preserves_its_parameter_metadata(seeded, tmp_path, monkeypatch):
    """The editor rebuilds `spec` from its own state, so anything the detail payload does not
    expose is silently DELETED on save. `choices`, `type` and a manifest default have no UI — they
    are JSON-level — so they have to survive the round trip untouched.

    Without this the first person to fix a typo in a parameterised scenario through the web editor
    would quietly strip every picker and override from it, and nothing would say so."""
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path))
    _param_scenario(tmp_path, parameters={
        "category": {"choices": ["fruit", "colour"], "description": "kind of word"},
        "target": {"default": "99", "type": "int", "min": 1, "max": 100},
    })
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        detail = c.get("/api/packages/param-demo").json()
        assert detail["parameters"]["category"]["choices"] == ["fruit", "colour"]
        assert detail["parameters"]["target"]["default"] == "99"

        # save it back exactly as the editor would, having changed only prose
        body = {
            "metadata": {"name": "param-demo", "description": detail["description"]},
            "spec": {
                "goals": detail["goals"], "guidelines": "edited",
                "termination": [{"type": "manual"}],
                "parameters": detail["parameters"],
            },
            "agents": [{"id": "orin", "model_category": "medium",
                        "persona": "You play for ${team_name}"}],
        }
        assert c.put("/api/packages/param-demo", json=body).status_code == 200

        after = c.get("/api/packages/param-demo").json()
        assert after["guidelines"] == "edited", "the edit landed"
        assert after["parameters"]["category"]["choices"] == ["fruit", "colour"], (
            "a picker must survive an unrelated edit"
        )
        assert after["parameters"]["target"]["default"] == "99"
        assert after["parameters"]["target"]["max"] == 100


def test_the_turns_override_keeps_everything_the_scenario_declared():
    """The launch modal offers exactly two knobs — on/off and the silence timeout — but the
    endpoint rebuilt `Turns()` from that one field, so every OTHER setting the scenario declared
    silently reverted to its schema default.

    debate-arena declares `min_rounds_before_verdict: 2` precisely so its judge cannot call a
    winner before both rounds finish. Launching it from the UI reset that to 0, and the run
    (debate-arena-6947dc) recorded `min_rounds: 0` in every TURN event. The judge duly ended the
    realm on scores for rounds it had never seen.

    policy/advance/enforcement/order happen to share their defaults with debate-arena, which is
    why this stayed invisible; a scenario that sets any of them non-default loses it outright.
    """
    from bearpit.core.schema import Turns
    from bearpit.gatekeeper.api import TurnsConfig, apply_turns_override

    declared = Turns(
        min_rounds_before_verdict=2, referee_cue="turn", retire_after_misses=3,
        silence_timeout_s=240,
    )
    merged = apply_turns_override(declared, TurnsConfig(enabled=True, silence_timeout_s=120))

    assert merged is not None
    assert merged.silence_timeout_s == 120, "the UI's own knob must still win"
    assert merged.min_rounds_before_verdict == 2, "the verdict guard was silently dropped"
    assert merged.referee_cue == declared.referee_cue
    assert merged.retire_after_misses == 3


def test_the_turns_override_can_still_turn_turns_off_and_on():
    from bearpit.core.schema import Turns
    from bearpit.gatekeeper.api import TurnsConfig, apply_turns_override

    declared = Turns(min_rounds_before_verdict=2)
    assert apply_turns_override(declared, TurnsConfig(enabled=False)) is None
    # a scenario with NO turns, switched on from the UI, gets the schema defaults + the knob
    fresh = apply_turns_override(None, TurnsConfig(enabled=True, silence_timeout_s=45))
    assert fresh is not None and fresh.silence_timeout_s == 45
    assert fresh.min_rounds_before_verdict == 0


class _StubPlatform:
    """Just enough for the image guard to find a container runtime, the way the real
    `RealmManager.platform.runtime` does."""

    def __init__(self, runtime):
        self.runtime = runtime


class _StubRuntime:
    def __init__(self, answer, exit_code=0):
        self._answer, self._exit = answer, exit_code

    def exec_python(self, container_id, code, *, timeout_s=30, user="10000"):
        return self._exit, self._answer


def _manager_with_image(answer, exit_code=0):
    mgr = FakeManager()
    mgr.platform = _StubPlatform(_StubRuntime(answer, exit_code))
    return mgr


def test_a_launch_is_refused_when_realmtools_is_not_running_this_code(seeded, tmp_path):
    """A stale realmtools image is the one failure here that announces itself with nothing: the
    realm runs, concludes, and writes a well-formed verdict computed by code nobody is looking at.
    It has happened twice, once turning an among-us run into a spurious "crew win" (#106).

    This test exists because the guard's first wiring reached for `mgr.runtime`, which does not
    exist — `getattr(..., None)` then made the whole check a silent no-op, and every other test
    still passed because a test double has no runtime either. Only a manager that DOES expose one
    can tell the difference.
    """
    (tmp_path / "project.json").write_text(json.dumps({
        "metadata": {"name": "plain"},
        "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "solo", "role": "participant",
                    "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}}],
    }))
    pkg = str(tmp_path)
    app = create_app(chron=seeded, manager=_manager_with_image("ffffffffffff\n"))
    with TestClient(app) as c:
        blocked = c.post("/api/realms", json={"package": pkg})
        allowed = c.post("/api/realms", json={"package": pkg, "allow_stale_image": True})
    assert 400 <= blocked.status_code < 500, blocked.text
    detail = blocked.json()["detail"]
    assert detail["container"] == "ffffffffffff"
    assert detail["host"] and detail["host"] != detail["container"]
    assert "allow_stale_image" in detail["hint"]
    assert allowed.status_code == 200, "the operator may still run the old build deliberately"


def test_an_image_that_matches_this_tree_launches_silently(seeded, tmp_path):
    """The guard only means something if an up-to-date deployment never sees it."""
    from bearpit.core.buildid import source_fingerprint

    (tmp_path / "project.json").write_text(json.dumps({
        "metadata": {"name": "plain"},
        "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "solo", "role": "participant",
                    "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}}],
    }))
    app = create_app(chron=seeded, manager=_manager_with_image(source_fingerprint() + "\n"))
    with TestClient(app) as c:
        r = c.post("/api/realms", json={"package": str(tmp_path)})
    assert r.status_code == 200, r.text


def test_an_image_too_old_to_answer_is_refused_too(seeded, tmp_path):
    """The check lives in the code it checks, so an image predating it cannot report at all. That
    failure is conclusive and must refuse, not pass."""
    (tmp_path / "project.json").write_text(json.dumps({
        "metadata": {"name": "plain"},
        "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "solo", "role": "participant",
                    "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}}],
    }))
    app = create_app(chron=seeded, manager=_manager_with_image(
        "ModuleNotFoundError: No module named 'bearpit.core.buildid'", exit_code=1))
    with TestClient(app) as c:
        r = c.post("/api/realms", json={"package": str(tmp_path)})
    assert 400 <= r.status_code < 500, r.text
    assert r.json()["detail"]["container"] is None


def test_the_preview_can_show_a_local_skill_not_just_the_builtins(seeded):
    """`skill_contents` is what the read-only scenario preview shows when you click a skill pill.
    It resolved a local skill from `<pkg>/skills/<ref>/SKILL.md` — a PROJECT-level path that no
    package has, because the loader reads local skills from `agents/<id>/skills/<ref>/`. So every
    local pill in the preview was clickable and empty, and only builtins ever showed text."""
    app = create_app(chron=seeded, manager=FakeManager())
    with TestClient(app) as c:
        r = c.get("/api/packages/poker-table")
    assert r.status_code == 200, r.text
    contents = r.json()["skill_contents"]
    assert "local:pot-odds" in contents, "a local skill must resolve for the preview"
    assert "multiway trap" in contents["local:pot-odds"], "and carry its real text"
    assert "builtin:competitor" in contents, "builtins still resolve"
