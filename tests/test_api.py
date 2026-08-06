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
        self.max_active = max_active

    def start(self, realm_id, project, *, require_mention=True):
        if len(self.active()) >= self.max_active:
            raise CapacityError(f"{self.max_active} realms already running")
        self.started.append((realm_id, len(project.agents)))
        self.projects[realm_id] = project

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
