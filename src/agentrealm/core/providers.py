"""Model-provider profiles + the category resolver.

A scenario's agent only declares a **capability tier** (`model_category`: small/medium/large). A
*provider profile* maps each tier to a concrete model + reasoning effort + costs + context window,
and carries the provider's keystore handle. The active provider (a global toggle) selects which
profile resolves. This is pure policy — the profiles' DEFAULTS live here; the user edits them in
Settings (persisted in `~/.agentrealm/platform.json`, see `gatekeeper.appstate`), and the resolver
runs at the single launch chokepoint (`Platform.run`), so switching pipelines never edits a
manifest. An agent may still pin an exact `ModelRef` override, which wins over its category.
"""

from __future__ import annotations

import copy
from typing import Any

from agentrealm.core.plugins import plugin_profiles
from agentrealm.core.schema import ModelCategory, ModelRef, Project

AZURE = "azure"
OPENAI = "openai"
ANTHROPIC = "anthropic"
OPENROUTER = "openrouter"
DEFAULT_PROVIDER = AZURE
CATEGORIES = (ModelCategory.SMALL, ModelCategory.MEDIUM, ModelCategory.LARGE)

# The seed provider tables. Each category → {model, effort, costs, context_length}. `api_key_ref`
# is the keystore handle the provider resolves to. Everything here is editable in Settings.
#
# A profile may also carry POLICY FIELDS, all optional and all off by default:
#
#   flat_rate: bool          the plan is fixed-price, so the per-token costs above are budget
#                            UNITS rather than money
#   min_budget_usd: float    floor for a too-tight per-agent cap when flat_rate — only ever raises
#   min_turn_seconds: float  floor for `turns.silence_timeout_s` — only ever raises
#   setup_hint: str          what Settings shows when this provider's keystore handle is missing
#
# They are data, not code, precisely because they are generic: a fixed-price plan and a slow local
# model are both things a user might have, and both are then configurable from Settings without
# writing a plugin. Transport quirks are the opposite case and live in `core.plugins`.
#
# NOTE ON THE SEED VALUES: model ids, per-token prices, and context windows are a snapshot, not a
# contract. Providers rename models and change prices; every field below is editable on the
# Settings page and the prices only ever meter YOUR budgets. Check them against your provider's
# current pricing page before you rely on a cap.
DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    AZURE: {
        "label": "Azure",
        "description": "Each agent's tier runs on your Azure OpenAI deployment (default).",
        "api_key_ref": "azure-main",
        # Azure GPT-5.4 effort support is uncertain, so seed no effort (behaves as today); the
        # reasoning_effort param is dropped by the proxy if the deployment rejects it.
        "categories": {
            "small":  {"model": "gpt-5.4-mini", "effort": None,
                       "input_cost_per_token": 1.5e-7, "output_cost_per_token": 6e-7,
                       "context_length": 128000},
            "medium": {"model": "gpt-5.4", "effort": None,
                       "input_cost_per_token": 5e-7, "output_cost_per_token": 4e-6,
                       "context_length": 128000},
            "large":  {"model": "gpt-5.4-pro", "effort": None,
                       "input_cost_per_token": 1.25e-6, "output_cost_per_token": 1e-5,
                       "context_length": 128000},
        },
    },
    OPENAI: {
        "label": "OpenAI",
        "description": "Each agent's tier runs on the OpenAI API.",
        "api_key_ref": "openai-main",
        "setup_hint": "run `arealm keys add openai-main --provider openai --api-key sk-...`",
        "categories": {
            "small":  {"model": "gpt-5.4-mini", "effort": None,
                       "input_cost_per_token": 1.5e-7, "output_cost_per_token": 6e-7,
                       "context_length": 128000},
            "medium": {"model": "gpt-5.4", "effort": None,
                       "input_cost_per_token": 5e-7, "output_cost_per_token": 4e-6,
                       "context_length": 128000},
            "large":  {"model": "gpt-5.4-pro", "effort": None,
                       "input_cost_per_token": 1.25e-6, "output_cost_per_token": 1e-5,
                       "context_length": 128000},
        },
    },
    ANTHROPIC: {
        "label": "Anthropic",
        "description": "Each agent's tier runs on the Anthropic API.",
        "api_key_ref": "anthropic-main",
        "setup_hint": (
            "run `arealm keys add anthropic-main --provider anthropic --api-key sk-ant-...`"
        ),
        "categories": {
            "small":  {"model": "claude-haiku-4-5", "effort": "low",
                       "input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
                       "context_length": 200000},
            "medium": {"model": "claude-sonnet-5", "effort": "medium",
                       "input_cost_per_token": 3e-6, "output_cost_per_token": 1.5e-5,
                       "context_length": 200000},
            "large":  {"model": "claude-opus-5", "effort": "high",
                       "input_cost_per_token": 1.5e-5, "output_cost_per_token": 7.5e-5,
                       "context_length": 200000},
        },
    },
    OPENROUTER: {
        "label": "OpenRouter",
        "description": "One key, many vendors — the easiest pipeline to start on.",
        "api_key_ref": "openrouter-main",
        "setup_hint": (
            "run `arealm keys add openrouter-main --provider openrouter "
            "--api-base https://openrouter.ai/api/v1 --api-key sk-or-...`"
        ),
        "categories": {
            "small":  {"model": "anthropic/claude-haiku-4.5", "effort": "low",
                       "input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
                       "context_length": 200000},
            "medium": {"model": "anthropic/claude-sonnet-5", "effort": "medium",
                       "input_cost_per_token": 3e-6, "output_cost_per_token": 1.5e-5,
                       "context_length": 200000},
            "large":  {"model": "openai/gpt-5.4", "effort": None,
                       "input_cost_per_token": 5e-7, "output_cost_per_token": 4e-6,
                       "context_length": 128000},
        },
    },
}


def default_providers() -> dict[str, dict[str, Any]]:
    """A fresh deep copy of the seed tables, with any installed plugin's profiles merged on top
    (safe to mutate / merge with user overrides). A plugin may add providers and may override a
    built-in one; it cannot delete one."""
    cfg = copy.deepcopy(DEFAULT_PROVIDERS)
    cfg.update(copy.deepcopy(plugin_profiles()))
    return cfg


def is_provider(name: str, providers: dict[str, dict[str, Any]] | None = None) -> bool:
    return name in (providers if providers is not None else default_providers())


def resolve_category_model(category: str, profile: dict[str, Any]) -> ModelRef:
    """Build the concrete ModelRef for a tier from a provider profile. Falls back to 'medium' then
    to the first defined category if the tier is missing from the profile."""
    cats = profile.get("categories", {})
    entry: dict[str, Any] = (
        cats.get(category) or cats.get("medium") or next(iter(cats.values()), {})
    )
    return ModelRef(
        provider=str(profile.get("name") or profile.get("_name") or ""),
        model=str(entry.get("model") or "unknown"),
        api_key_ref=str(profile.get("api_key_ref") or "azure-main"),
        input_cost_per_token=entry.get("input_cost_per_token"),
        output_cost_per_token=entry.get("output_cost_per_token"),
        context_length=entry.get("context_length"),
        effort=entry.get("effort"),
    )


def _profile(
    provider: str, providers: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The active profile, or an empty one for an unknown provider (every policy field then reads
    as absent, so each transform is a no-op)."""
    table = providers if providers is not None else default_providers()
    profile = table.get(provider)
    return profile if isinstance(profile, dict) else {}


def _positive_float(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def raise_budgets_for_flat_rate_provider(
    project: Project, provider: str, providers: dict[str, dict[str, Any]] | None = None
) -> Project:
    """Lift a too-tight per-agent cap on a FLAT-RATE pipeline (`flat_rate` + `min_budget_usd`).

    On a fixed-price plan the per-token costs in the profile are budget UNITS, not money, so a cap
    sized for a metered API is meaningless — and actively dangerous. When it bites mid-realm
    LiteLLM answers 429, the runtime retries in a loop, and every failure lands in the room:
    debate-1 died that way, drowning in 2,540 copies of "the model provider is rate-limiting
    requests" after one agent hit a $2 cap. The floor's only job is to stop a runaway loop, so it
    must sit well clear of a healthy realm.

    Only ever RAISES, and only when the profile declares it. A metered provider keeps the author's
    cap exactly as written, because there it is real money."""
    profile = _profile(provider, providers)
    floor = _positive_float(profile.get("min_budget_usd"))
    if not profile.get("flat_rate") or floor is None:
        return project
    agents = []
    for a in project.agents:
        cap = a.budget.max_usd
        if cap is not None and cap < floor:
            agents.append(a.model_copy(update={
                "budget": a.budget.model_copy(update={"max_usd": floor})
            }))
        else:
            agents.append(a)
    return project.model_copy(update={"agents": agents})


def pace_turns_for_provider(
    project: Project, provider: str, providers: dict[str, dict[str, Any]] | None = None
) -> Project:
    """Give the floor-holder more time on a slow pipeline (`min_turn_seconds`).

    Where a turn is several model calls — the agent reads a skill, reasons, then responds — plus
    process latency, a tight (~120s) window can lapse mid-turn on the cold-start round: the floor
    passes before the agent posts (diagnosed in among-us). This only RAISES the silence timeout, so
    a responsive agent still advances on its own post and a healthy turn is never slowed. A no-op
    for providers that declare no floor, or when the scenario already allows enough time."""
    floor = _positive_float(_profile(provider, providers).get("min_turn_seconds"))
    turns = project.spec.turns
    if floor is None or turns is None or turns.silence_timeout_s >= floor:
        return project
    spec = project.spec.model_copy(
        update={"turns": turns.model_copy(update={"silence_timeout_s": floor})}
    )
    return project.model_copy(update={"spec": spec})


def resolve_project(
    project: Project, provider: str, providers: dict[str, dict[str, Any]] | None = None
) -> Project:
    """A copy of `project` with every agent's model resolved for the active provider. An agent with
    an explicit `model` override keeps it; otherwise its `model_category` is resolved via the active
    provider's table. Unknown provider → project returned unchanged."""
    table = providers if providers is not None else default_providers()
    profile = table.get(provider)
    if profile is None:
        return project
    profile = {**profile, "name": provider}  # stamp the name so ModelRef.provider is set
    agents = []
    for a in project.agents:
        if a.model is not None:  # an explicit override wins over the category
            agents.append(a)
        else:
            agents.append(a.model_copy(update={"model": resolve_category_model(a.model_category,
                                                                               profile)}))
    return project.model_copy(update={"agents": agents})
