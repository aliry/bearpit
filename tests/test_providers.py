"""Provider category tables, the resolver, and the two policy transforms.

Everything here is provider-agnostic on purpose: the transforms are driven by POLICY FIELDS on a
profile (`flat_rate`, `min_budget_usd`, `min_turn_seconds`), never by a provider's name. A fake
profile stands in for whatever pipeline declares them, which is exactly how a contributed provider
reaches this code.
"""

from fakes import FLAT
from fakes import FLAT_RATE_PROFILE as _FLAT_PROFILE
from fakes import flat_rate_table as _table

from agentrealm.core.providers import (
    AZURE,
    default_providers,
    is_provider,
    pace_turns_for_provider,
    raise_budgets_for_flat_rate_provider,
    resolve_category_model,
    resolve_project,
)
from agentrealm.core.schema import (
    AgentSpec,
    Budget,
    ModelCategory,
    ModelRef,
    Project,
    ProjectMeta,
    ProjectSpec,
    Turns,
)


def _project(**kw):
    return Project(metadata=ProjectMeta(name="p"), agents=[
        AgentSpec(id="lead", model_category=ModelCategory.LARGE),
        AgentSpec(id="helper", model_category=ModelCategory.SMALL),
        AgentSpec(id="mid"),  # defaults to medium
    ], **kw)


# --- the table + the resolver -------------------------------------------------------------------
def test_every_shipped_provider_has_all_three_categories():
    cfg = default_providers()
    assert AZURE in cfg
    for name, prof in cfg.items():
        assert set(prof["categories"]) == {"small", "medium", "large"}, name
        assert prof["api_key_ref"], name
        for cat, entry in prof["categories"].items():
            assert entry.get("model"), f"{name}/{cat}"
            # budgets are meaningless without per-token costs — LiteLLM cannot infer them
            assert entry.get("input_cost_per_token") is not None, f"{name}/{cat}"
            assert entry.get("output_cost_per_token") is not None, f"{name}/{cat}"


def test_resolve_category_model_builds_modelref_with_effort_and_costs():
    prof = {**_FLAT_PROFILE, "name": FLAT}
    m = resolve_category_model("large", prof)
    assert m.provider == FLAT and m.model == "fake-l"
    assert m.api_key_ref == "fake-main" and m.effort == "high"
    assert m.input_cost_per_token == 3e-6 and m.context_length == 200000


def test_resolve_category_falls_back_to_medium_for_an_unknown_tier():
    prof = {**_FLAT_PROFILE, "name": FLAT}
    assert resolve_category_model("enormous", prof).model == "fake-m"


def test_resolve_project_maps_each_agent_by_category():
    p = resolve_project(_project(), FLAT, _table())
    by = {a.id: a.require_model() for a in p.agents}
    assert by["lead"].model == "fake-l" and by["lead"].effort == "high"
    assert by["helper"].model == "fake-s" and by["helper"].effort == "low"
    assert by["mid"].model == "fake-m" and by["mid"].effort == "medium"
    assert all(m.api_key_ref == "fake-main" for m in by.values())


def test_resolve_project_azure_uses_azure_table():
    p = resolve_project(_project(), AZURE)
    by = {a.id: a.require_model() for a in p.agents}
    assert by["lead"].model == "gpt-5.4-pro" and by["helper"].model == "gpt-5.4-mini"
    assert by["mid"].model == "gpt-5.4"
    assert all(m.api_key_ref == "azure-main" for m in by.values())


def test_explicit_override_wins_over_category():
    override = ModelRef(provider="azure", model="custom-x", api_key_ref="azure-main",
                        input_cost_per_token=1e-7, output_cost_per_token=1e-7)
    proj = Project(metadata=ProjectMeta(name="p"), agents=[
        AgentSpec(id="pinned", model_category=ModelCategory.SMALL, model=override),
    ])
    p = resolve_project(proj, FLAT, _table())
    assert p.agents[0].require_model().model == "custom-x"  # override kept, not fake-s


def test_resolve_project_unknown_provider_unchanged():
    proj = _project()
    assert resolve_project(proj, "gemini") is proj


def test_custom_provider_config_overrides_default():
    cfg = _table()
    cfg[FLAT]["categories"]["large"] = {
        "model": "fake-xl", "effort": "max",
        "input_cost_per_token": 1.5e-5, "output_cost_per_token": 7.5e-5, "context_length": 200000,
    }
    p = resolve_project(_project(), FLAT, cfg)
    assert p.agents[0].require_model().model == "fake-xl"  # lead is large
    assert p.agents[0].require_model().effort == "max"


def test_is_provider():
    assert is_provider(AZURE)
    assert is_provider(FLAT, _table())
    assert not is_provider("gemini")


# --- min_turn_seconds ----------------------------------------------------------------------------
def _turns_project(timeout):
    return Project(metadata=ProjectMeta(name="p"),
                   spec=ProjectSpec(turns=Turns(silence_timeout_s=timeout)),
                   agents=[AgentSpec(id="a"), AgentSpec(id="b")])


def test_a_declared_turn_floor_raises_a_tight_window():
    p = pace_turns_for_provider(_turns_project(120.0), FLAT, _table())
    assert p.spec.turns.silence_timeout_s == 240.0  # a slow pipeline gets headroom


def test_turn_floor_never_lowers_and_is_a_noop_without_one():
    # a provider that declares no floor keeps the scenario's snappy window
    assert pace_turns_for_provider(
        _turns_project(120.0), AZURE).spec.turns.silence_timeout_s == 120
    # a scenario already allowing enough time is left alone even on a floored pipeline
    assert pace_turns_for_provider(
        _turns_project(400.0), FLAT, _table()).spec.turns.silence_timeout_s == 400


def test_turn_floor_is_a_noop_without_turns():
    proj = Project(metadata=ProjectMeta(name="p"), agents=[AgentSpec(id="a")])
    assert pace_turns_for_provider(proj, FLAT, _table()) is proj


def test_turn_floor_ignores_a_malformed_value():
    cfg = _table()
    cfg[FLAT]["min_turn_seconds"] = "soon"
    assert pace_turns_for_provider(
        _turns_project(120.0), FLAT, cfg).spec.turns.silence_timeout_s == 120


# --- flat_rate + min_budget_usd -------------------------------------------------------------------
def _budget_project():
    def agent(aid, usd):
        model = ModelRef(
            provider="azure", model="m", api_key_ref="k",
            # a cap can only be enforced when the model declares per-token costs
            input_cost_per_token=3e-6, output_cost_per_token=1.5e-5,
        )
        return AgentSpec(id=aid, model=model, budget=Budget(max_usd=usd))

    return Project(metadata=ProjectMeta(name="p"),
                   agents=[agent("tight", 2.0), agent("generous", 50.0)])


def test_a_flat_rate_pipeline_lifts_a_too_tight_budget_cap():
    """On a fixed-price plan the per-token costs are budget UNITS, not money. A $2 cap sized for a
    metered API is meaningless there, and lethal mid-realm: when it bites, LiteLLM answers 429, the
    runtime retries in a loop and posts every failure into the room. debate-1 died exactly that way
    — one agent hit a $2 cap and the realm drowned in 2,540 copies of "the model provider is
    rate-limiting requests"."""
    lifted = raise_budgets_for_flat_rate_provider(_budget_project(), FLAT, _table())
    caps = {a.id: a.budget.max_usd for a in lifted.agents}
    assert caps["tight"] == 25.0      # raised clear of a healthy realm
    assert caps["generous"] == 50.0   # an ample cap is left exactly as the author wrote it


def test_a_metered_provider_never_has_its_caps_touched():
    project = _budget_project()
    metered = raise_budgets_for_flat_rate_provider(project, AZURE)
    assert {a.id: a.budget.max_usd for a in metered.agents} == {"tight": 2.0, "generous": 50.0}


def test_flat_rate_without_a_floor_changes_nothing():
    cfg = _table()
    del cfg[FLAT]["min_budget_usd"]
    kept = raise_budgets_for_flat_rate_provider(_budget_project(), FLAT, cfg)
    assert {a.id: a.budget.max_usd for a in kept.agents} == {"tight": 2.0, "generous": 50.0}


def test_a_floor_without_flat_rate_changes_nothing():
    # min_budget_usd alone must not touch real money — flat_rate is what licenses the lift
    cfg = _table()
    cfg[FLAT]["flat_rate"] = False
    kept = raise_budgets_for_flat_rate_provider(_budget_project(), FLAT, cfg)
    assert {a.id: a.budget.max_usd for a in kept.agents} == {"tight": 2.0, "generous": 50.0}
