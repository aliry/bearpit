"""Schema validation: the security invariants and structural rules that must hold."""

import pytest
from pydantic import ValidationError

from agentrealm.core.schema import (
    AgentRole,
    AgentSpec,
    ModelRef,
    Project,
    ProjectMeta,
    ProjectSpec,
    TerminationCondition,
    Turns,
    parse_duration,
)


def _model(**kw):
    base = {"provider": "anthropic", "model": "claude-opus-4-8", "api_key_ref": "anthropic-main"}
    return {**base, **kw}


def test_parse_duration():
    assert parse_duration("30s") == 30
    assert parse_duration("10m") == 600
    assert parse_duration("6h") == 21600
    assert parse_duration("2d") == 172800
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_api_key_ref_rejects_secrets():
    ModelRef(**_model())  # a handle is fine
    with pytest.raises(ValidationError):
        ModelRef(**_model(api_key_ref="sk-ant-api03-abcdef0123456789"))
    with pytest.raises(ValidationError):
        ModelRef(**_model(api_key_ref="AKIA1234567890ABCDEF1234567890"))


def test_unknown_field_forbidden():
    with pytest.raises(ValidationError):
        AgentSpec(id="a", model=_model(), typo=True)  # type: ignore[call-arg]


def test_agent_id_rules():
    AgentSpec(id="vela", model=_model())
    with pytest.raises(ValidationError):
        AgentSpec(id="_ghost", model=_model())  # leading underscore (caveat C1)
    with pytest.raises(ValidationError):
        AgentSpec(id="Vela", model=_model())  # uppercase


def test_referee_fields_are_referee_only():
    ref = AgentSpec(id="themis", role=AgentRole.REFEREE, model=_model(), rubric="score fairly")
    assert ref.powers is not None  # defaulted for referees
    with pytest.raises(ValidationError):
        AgentSpec(id="vela", role=AgentRole.PARTICIPANT, model=_model(), rubric="nope")


def test_project_integrity():
    meta = ProjectMeta(name="p")
    a = AgentSpec(id="vela", model=_model())
    b = AgentSpec(id="orin", role=AgentRole.REFEREE, model=_model())
    proj = Project(metadata=meta, agents=[a, b])
    assert proj.referee is not None and proj.referee.id == "orin"
    with pytest.raises(ValidationError):
        Project(metadata=meta, agents=[a, a])  # dup ids
    with pytest.raises(ValidationError):
        Project(metadata=meta, agents=[
            AgentSpec(id="r1", role=AgentRole.REFEREE, model=_model()),
            AgentSpec(id="r2", role=AgentRole.REFEREE, model=_model()),
        ])  # two referees


def test_effective_termination_honors_verdict_ends_realm():
    from agentrealm.core.schema import RefereePowers, TerminationKind

    meta = ProjectMeta(name="p")
    part = AgentSpec(id="p1", model=_model())
    ref_on = AgentSpec(id="judge", role=AgentRole.REFEREE, model=_model(),
                       powers=RefereePowers(verdict_ends_realm=True))
    # power ON + no referee_verdict declared -> one is synthesized so rule() ends the realm
    dur = TerminationCondition(type="duration", limit="1h")
    proj = Project(metadata=meta, agents=[part, ref_on], spec=ProjectSpec(termination=[dur]))
    kinds = [c.type for c in proj.effective_termination]
    assert TerminationKind.REFEREE_VERDICT in kinds and TerminationKind.DURATION in kinds

    # power OFF -> nothing synthesized
    ref_off = AgentSpec(id="judge", role=AgentRole.REFEREE, model=_model(),
                        powers=RefereePowers(verdict_ends_realm=False))
    proj_off = Project(metadata=meta, agents=[part, ref_off])
    assert not any(c.type == TerminationKind.REFEREE_VERDICT
                   for c in proj_off.effective_termination)

    # already declared -> not duplicated
    proj_dup = Project(metadata=meta, agents=[part, ref_on],
                       spec=ProjectSpec(termination=[TerminationCondition(type="referee_verdict")]))
    assert sum(c.type == TerminationKind.REFEREE_VERDICT
               for c in proj_dup.effective_termination) == 1


def test_termination_requires_fields_by_type():
    TerminationCondition(type="duration", limit="6h")
    TerminationCondition(type="file", path="shared/answer.md", content_match="FINAL")
    with pytest.raises(ValidationError):
        TerminationCondition(type="duration")  # missing limit
    with pytest.raises(ValidationError):
        TerminationCondition(type="file")  # missing path


def test_turns_defaults_are_mvp():
    t = Turns()
    assert t.policy == "one-at-a-time" and t.advance == "one-message"
    assert t.enforcement == "physics" and t.order == "roster"
    assert t.silence_timeout_s == 90.0
    # a spec with no turns block = turns disabled (default)
    assert ProjectSpec().turns is None
    assert ProjectSpec(turns=Turns()).turns is not None


def test_turns_rejects_unimplemented_options():
    for bad in [
        {"advance": "quiet-gap"},
        {"advance": "time-slice"},
        {"enforcement": "law"},
        {"order": "random"},
        {"silence_timeout_s": 0},
        {"silence_timeout_s": -5},
    ]:
        with pytest.raises(ValidationError):
            Turns(**bad)


def test_budget_cap_requires_per_token_costs():
    from agentrealm.core.schema import AgentSpec
    no_cost = {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}
    with_cost = {**no_cost, "input_cost_per_token": 1e-7, "output_cost_per_token": 6e-7}
    # a cap without costs would silently no-op -> rejected
    with pytest.raises(ValidationError):
        AgentSpec(id="a", model=no_cost, budget={"max_usd": 2.0})
    with pytest.raises(ValidationError):
        AgentSpec(id="a", model=no_cost, budget={"max_tokens": 1000})
    # with costs, fine; no cap, costs not required
    AgentSpec(id="a", model=with_cost, budget={"max_usd": 2.0})
    AgentSpec(id="a", model=no_cost)


def test_budget_termination_scope_validated():
    TerminationCondition(type="budget_exhausted", scope="realm_total")  # valid
    TerminationCondition(type="budget_exhausted")  # None ok (defaults any_agent)
    with pytest.raises(ValidationError):
        TerminationCondition(type="budget_exhausted", scope="whole_team")  # no silent fallback


def test_effective_budget_combines_usd_and_tokens():
    from agentrealm.core.schema import AgentSpec
    from agentrealm.ledger.ledger import _effective_budget_usd
    m = {"provider": "azure", "model": "m", "api_key_ref": "azure-main",
         "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}
    # max_tokens 1000 * higher cost 2e-6 = 0.002; min(max_usd 0.01, 0.002) = 0.002
    a = AgentSpec(id="a", model=m, budget={"max_usd": 0.01, "max_tokens": 1000})
    assert _effective_budget_usd(a) == pytest.approx(0.002)
    assert _effective_budget_usd(AgentSpec(id="a", model=m, budget={"max_usd": 0.5})) == 0.5
    assert _effective_budget_usd(AgentSpec(id="a", model=m)) is None


def test_mechanic_ruleset_validated():
    from agentrealm.core.schema import Mechanic
    Mechanic(kind="sealed-submit", ruleset="low-bid")       # built-in ok
    Mechanic(kind="sealed-submit", ruleset="custom:mine")   # custom: escape ok
    Mechanic(kind="sealed-submit")                          # None ok
    with pytest.raises(ValidationError):
        Mechanic(kind="sealed-submit", ruleset="borda")     # unknown -> rejected at parse


def test_a_bare_hex_key_is_rejected_as_a_handle():
    """A 32-char hex string is the shape of an Azure OpenAI key. The old detector needed
    len>=40 AND >=8 digits, so it let one through — baking a live secret into a package."""
    from agentrealm.core.schema import ModelRef

    for handle in ("azure-main", "anthropic-main", "themis-scorer", "k"):
        ModelRef(provider="azure", model="m", api_key_ref=handle)  # legit handles pass
    for key in ("0a1b2c3d4e5f60718293a4b5c6d7e8f9",              # 32 bare hex (Azure shape)
                "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"):         # same with dashes
        with pytest.raises(ValueError, match="handle"):
            ModelRef(provider="azure", model="m", api_key_ref=key)


def test_a_per_round_dm_quota_needs_a_turns_block():
    """max_per_round is per-ROUND. Without a turns block the runner has no round, so it would
    silently become a whole-run cap under a 'per round' name — reject it at load."""
    from agentrealm.core.schema import (
        AgentSpec,
        ModelRef,
        PrivateMessaging,
        Project,
        ProjectMeta,
        ProjectSpec,
    )

    def agent(aid, quota=0):
        return AgentSpec(
            id=aid, model=ModelRef(provider="azure", model="m", api_key_ref="k"),
            private_messaging=PrivateMessaging(enabled=True, max_per_round=quota),
        )

    # with turns: fine
    Project(metadata=ProjectMeta(name="p"), spec=ProjectSpec(turns={}),
            agents=[agent("a", 2), agent("b", 2)])
    # without turns: rejected
    with pytest.raises(ValueError, match="needs a `turns` block"):
        Project(metadata=ProjectMeta(name="p"), agents=[agent("a", 2)])
