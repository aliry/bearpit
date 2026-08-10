"""The project-package schema (architecture §13 + §13.5).

The Pydantic `Project` model is the single internal representation of a project. Its
canonical on-disk form is a portable package folder (see `package.py`); a flat
project.json/yaml is the trivial case. Every model forbids unknown fields so manifest
typos fail loudly at validation time, and every field carries a `description` so the same
definitions feed the JSON Schema we export for editor/CI validation.

Security invariant: secrets never live in the schema. Credentials are referenced by
*handle* (`api_key_ref`); a validator rejects anything that looks like a real key.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# Reusable bounded string types (item-level bounds for list fields + shorthands for scalars).
# max_length caps input to sane sizes so no field accepts, e.g., a 1000-char agent name.
ShortText = Annotated[str, StringConstraints(max_length=120)]  # names, ids-as-text, handles
LineText = Annotated[str, StringConstraints(max_length=2000)]  # one-liners (descriptions)
TagText = Annotated[str, StringConstraints(max_length=40)]  # a single tag
GoalText = Annotated[str, StringConstraints(max_length=1000)]  # a single goal
LongText = Annotated[str, StringConstraints(max_length=50000)]  # persona/rubric/guidelines bodies
# One tool grant: `family_verb`, lowercase (ADR-004). NOT dotted: a dot survives MCP and then dies
# at the model, whose function-calling API allows only [A-Za-z0-9_-] in a tool name. Shape only —
# whether the tool is INSTALLED
# is checked by `core.tools.check_grants`, deliberately not here: existence depends on which
# packages this machine happens to have, and a manifest must stay loadable, viewable and
# exportable on a machine that lacks the plugin.
ToolName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]*_[a-z][a-z0-9_]*$")]

_DURATION_RE = re.compile(r"^\d+(\.\d+)?\s*(s|m|h|d)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
BUDGET_SCOPES = ("any_agent", "all_agents", "realm_total")  # budget_exhausted termination scopes
BUILTIN_RULESETS = frozenset(
    {"dominance", "high-bid", "low-bid", "plurality", "majority", "unanimous"}
)  # GENERIC deterministic tally rulesets (no single game's rules); parity with realmtools.tally


def parse_duration(text: str) -> float:
    """Parse a human duration string into seconds.

    Accepts `<number><unit>` where unit is s/m/h/d, e.g. '30s' -> 30.0, '6h' -> 21600.0.
    Raises ValueError on anything malformed (used by the validators below and by Warden).
    """
    m = _DURATION_RE.match(text.strip())
    if not m:
        raise ValueError(f"invalid duration {text!r} (use e.g. '30s', '10m', '6h', '2d')")
    num = float(text.strip()[:-1].strip())
    return num * _UNIT_SECONDS[m.group(2)]


class _Base(BaseModel):
    """Shared base for every schema model.

    `extra="forbid"` makes an unknown/misspelled field a hard validation error instead of a
    silently-ignored typo. `populate_by_name` lets aliased fields (e.g. `apiVersion`) also be
    set by their Python name.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# =============================================================================
# Enums — each value is a distinct, behaviour-affecting choice.
# =============================================================================
class AgentRole(StrEnum):
    """What treatment the platform gives an agent. Only REFEREE changes behaviour;
    competitive-vs-cooperative is a persona/goals matter, not a role (motive-agnostic, FR-12).
    """

    PARTICIPANT = "participant"  # a regular agent: normal loadout, subject to the referee
    REFEREE = "referee"  # privileged: Arbiter loadout, rubric/powers, read-all, judges


class ModelCategory(StrEnum):
    """A provider-agnostic capability tier an agent asks for. The active provider's Settings table
    maps each tier to a concrete model + effort (see bearpit.core.providers). This is the ONLY
    model control most agents need; `ModelRef` is an optional per-agent exact override."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


# Valid reasoning-effort levels.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class MemoryMode(StrEnum):
    """What Forge does with an agent's HERMES_HOME volume (memory + self-authored skills)
    between realms — a real provisioning branch, not just a label.
    """

    EPHEMERAL = "ephemeral"  # fresh volume per realm: blank slate, fair/reproducible runs
    PERSISTENT = "persistent"  # reused identity-keyed volume: memory+skills accumulate.
    # NB: PERSISTENT (lineage) is a v3 feature (#24); the MVP honours EPHEMERAL only.


class OnExhausted(StrEnum):
    """What happens to an agent when its budget cap is hit (enforced by Ledger/Warden)."""

    STARVE = "starve"  # model calls fail; the container keeps running (may idle/retry)
    STARVE_THEN_KILL = "starve_then_kill"  # starve, then stop after the budget grace_period
    KILL = "kill"  # stop the agent immediately on exhaustion


class SkillSource(StrEnum):
    """Where a skill (SKILL.md role/capability guidance, §12.5) is fetched from."""

    BUILTIN = "builtin"  # the platform's role/capability library (#37), e.g. 'agent-basics'
    LOCAL = "local"  # a SKILL.md dir shipped inside this package's skills/ folder
    GH = "gh"  # gh://org/repo@ref — remote, pinned+sandboxed; deferred to v2/v3 (#38)


class EgressTier(StrEnum):
    """An agent container's outbound network access — physics at the container boundary."""

    NONE = "none"  # no network at all
    MODEL_ONLY = "model_only"  # only the LiteLLM proxy is reachable (default)
    ALLOWLIST = "allowlist"  # proxy + hosts in `Environment.egress_allowlist`
    OPEN = "open"  # full internet (internet-on realms are expected — FR-9b)


class TerminationKind(StrEnum):
    """The kinds of condition that can conclude a realm (first match wins; §11, FR-8)."""

    DURATION = "duration"  # wall-clock limit reached
    FILE = "file"  # a file matching path/content appears in the shared folder
    MESSAGE = "message"  # a message matching a pattern is posted on a channel
    BUDGET_EXHAUSTED = "budget_exhausted"  # agent budgets exhausted per `scope`
    REFEREE_VERDICT = "referee_verdict"  # the referee issues a concluding verdict
    STALL = "stall"  # no agent has spoken for `limit` — the realm is stuck; end deterministically
    MANUAL = "manual"  # operator kill switch (always implicitly available)
    # every non-referee participant's container has been stopped (killed or eliminated) — nobody is
    # left who could act. Always implicitly available, like `manual`: an empty realm cannot make
    # progress, so this is physics, not a rule an author has to remember to declare.
    NO_ACTIVE_PARTICIPANTS = "no_active_participants"


class MechanicKind(StrEnum):
    """Deterministic, platform-adjudicated interaction primitives (§9.5).

    MVP ships SEALED_SUBMIT only; verifiable-draw / turn-token / custom scorers are v2 (#31).
    """

    SEALED_SUBMIT = "sealed-submit"  # hidden simultaneous submission + reveal + tally


class TurnPolicy(StrEnum):
    """How turns are structured when turn-taking is enabled (turn-management spec)."""

    ONE_AT_A_TIME = "one-at-a-time"  # MVP: exactly one participant holds the floor


class TurnAdvance(StrEnum):
    """What ends a participant's turn and passes the floor."""

    ONE_MESSAGE = "one-message"  # MVP: one message, then the floor passes
    QUIET_GAP = "quiet-gap"  # future: floor passes after N seconds of silence
    TIME_SLICE = "time-slice"  # future: fixed duration per turn


class TurnEnforcement(StrEnum):
    """How the one-at-a-time rule is enforced."""

    PHYSICS = "physics"  # MVP: Matrix power levels — off-turn posts refused by the homeserver
    LAW = "law"  # future: mention-only + referee penalty (not enforced by the bus)


class TurnOrder(StrEnum):
    """The order in which participants take the floor."""

    ROSTER = "roster"  # MVP: round-robin in roster (project agent) order
    RANDOM = "random"  # future


class TurnCue(StrEnum):
    """When the system pushes a turn cue to the referee. Scenario-dependent: a debate referee
    judges per round; a turn-by-turn game referee may act on every move."""

    ROUND = "round"  # notify the referee when a full round completes (default)
    TURN = "turn"  # notify the referee on every turn change (per-turn refereeing)
    NONE = "none"  # no push — the referee reads turn_status() on its own


# =============================================================================
# Leaf models
# =============================================================================
# NOT IMPLEMENTED, and they now say so at load time rather than being silently ignored:
#   SkillSource.GH        — remote pinned+sandboxed skills (v2/v3)
#   EgressTier.ALLOWLIST  — per-host egress needs a real egress proxy; NONE/MODEL_ONLY/OPEN work
# A manifest that asks for one gets a hard validation error. Silently ignoring it is how an author
# ends up believing their agents are sandboxed to two hosts when they have the open internet.
_UNIMPLEMENTED = "not implemented yet"


class SkillRef(_Base):
    """A reference to one skill an agent should be seeded with (§12.5)."""

    source: SkillSource = Field(description="Where the skill comes from: builtin | local | gh.")
    ref: str = Field(
        description="Identifier within the source: a builtin name (e.g. 'agent-basics'), a "
        "local path relative to a skills/ folder, or 'gh://org/repo@ref'."
    )

    @model_validator(mode="after")
    def _reject_unimplemented_source(self) -> SkillRef:
        if self.source == SkillSource.GH:
            raise ValueError(f"skill source 'gh' is {_UNIMPLEMENTED}; use 'builtin' or 'local'")
        return self

class ModelRef(_Base):
    """Which model an agent uses. The key is referenced by handle and resolved at run time —
    never embedded (secrets stay out of the portable package, §13.5)."""

    provider: str = Field(
        min_length=1, max_length=60,
        description="Model provider, e.g. 'anthropic', 'openai', 'azure'.")
    model: str = Field(
        min_length=1, max_length=120,
        description="Model or deployment id, e.g. 'claude-opus-4-8'.")
    api_key_ref: str = Field(
        min_length=1, max_length=120,
        description="A credential HANDLE (e.g. 'anthropic-main'), NOT a secret. The runner "
        "resolves it from its keystore/Ledger; a validator rejects real-key shapes."
    )
    input_cost_per_token: float | None = Field(
        default=None, ge=0, le=1,
        description="USD per input token. Required for spend tracking on a custom route "
        "(LiteLLM cannot infer it — spike S3 finding F4).",
    )
    output_cost_per_token: float | None = Field(
        default=None, ge=0, le=1, description="USD per output token. See input_cost_per_token."
    )
    context_length: int | None = Field(
        default=None, ge=1, le=100_000_000,
        description="The model's context window in tokens. null = a conservative default; set it "
        "to match the actual model so the runtime doesn't truncate or under-use the window.",
    )
    effort: str | None = Field(
        default=None,
        description="Reasoning-effort level (low/medium/high/xhigh/max) for reasoning models; "
        "null = the model default. How it reaches the model is the provider's business — most "
        "send it as a request parameter the endpoint may ignore.",
    )

    @field_validator("effort")
    @classmethod
    def _valid_effort(cls, v: str | None) -> str | None:
        if v is not None and v not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {EFFORT_LEVELS} or null, got {v!r}")
        return v

    @field_validator("api_key_ref")
    @classmethod
    def _not_a_secret(cls, v: str) -> str:
        prefixes = ("sk-", "sk_", "akia", "asia", "agpa", "ghp_", "gho_", "xoxb-", "aiza")
        stripped = v.replace("-", "").replace("_", "")
        # a long, whitespace-free, high-entropy string is almost certainly a real key, not a handle.
        # The old gate needed len>=40 AND >=8 digits, which let a bare 32-char hex key through — the
        # exact shape of an Azure key — silently baking a live secret into a shared package.
        long_token = len(v) >= 40 and not any(c.isspace() for c in v)
        looks_hex = (
            len(stripped) >= 24 and not any(c.isspace() for c in v)
            and all(c in "0123456789abcdefABCDEF" for c in stripped)
        )
        if v.lower().startswith(prefixes) or long_token or looks_hex:
            raise ValueError(
                "api_key_ref must be a handle (e.g. 'anthropic-main'), not a real key — "
                "secrets never live in the package (§13.5)"
            )
        return v


class Budget(_Base):
    """Per-agent spend cap and the policy for what happens when it's exhausted (Ledger, §9)."""

    max_usd: float | None = Field(
        default=None, ge=0, le=1_000_000, description="Hard USD spend cap; null = uncapped.")
    max_tokens: int | None = Field(
        default=None, ge=0, le=10_000_000_000, description="Hard token cap; null = uncapped.")
    on_exhausted: OnExhausted = Field(
        default=OnExhausted.STARVE, description="Action when the cap is hit (see OnExhausted)."
    )
    grace_period: str | None = Field(
        default=None, max_length=20,
        description="For starve_then_kill: wait before stopping, e.g. '10m'."
    )

    @field_validator("grace_period")
    @classmethod
    def _valid_grace(cls, v: str | None) -> str | None:
        if v is not None:
            parse_duration(v)
        return v


class RefereePowers(_Base):
    """Which privileged capabilities a referee is granted (referee-only; §10)."""

    read_dms: bool = Field(
        default=False, description="May the referee read private agent-to-agent DMs?"
    )
    inspect_private_fs: Literal["none", "read_only"] = Field(
        default="none", description="Read-only inspection of agents' private filesystems, or none."
    )
    verdict_ends_realm: bool = Field(
        default=False, description="Does the referee's verdict conclude the realm?"
    )


class PrivateMessaging(_Base):
    """Whether an agent may exchange private side-channel messages with others (per-agent gate on
    top of Environment.allow_side_channels). Platform-brokered so the operator can observe them."""

    enabled: bool = Field(
        default=False, description="May this agent open private channels with other participants."
    )
    include_referee: bool = Field(
        default=False,
        description="Sub-option: may also privately message the referee (requires enabled)."
    )
    # A FACTION needs a private room shared by its members and by nobody else. Without this the
    # permission is all-or-nothing — an agent that may DM anyone may DM EVERYONE — so a hidden team
    # (the impostors in among-us, a cartel, an alliance) could not exist without also handing every
    # outsider a private line to a conspirator. Empty = any peer the roster permits (the default,
    # unchanged). The room is the gate: Herald only creates rooms for permitted pairs, so this is
    # physics, not an instruction the agent could ignore.
    peers: list[ShortText] = Field(
        default_factory=list, max_length=50,
        description="Restrict private messaging to these agent ids (empty = any permitted peer).",
    )
    # Two always-on agents alone in a private room ACK each other forever: the Commons has floor
    # control, a DM room has none, so every delivered message provokes a reply which provokes a
    # reply (among-us-sim1: the two impostors exchanged 25 messages of "Copy"/"Agreed" in one
    # round). A quota is the physics fix — law ("don't send bare acknowledgements") does not hold.
    # 0 = unlimited (the default, unchanged).
    max_per_round: int = Field(
        default=0, ge=0, le=100,
        description="Max private messages this agent may SEND per round (0 = unlimited). Needs a "
                    "`turns` block — without a round there is nothing to reset it, so it would "
                    "silently become a whole-run cap.",
    )

    @model_validator(mode="after")
    def _referee_needs_enabled(self) -> PrivateMessaging:
        if self.include_referee and not self.enabled:
            raise ValueError("private_messaging.include_referee requires enabled=true")
        if self.peers and not self.enabled:
            raise ValueError("private_messaging.peers requires enabled=true")
        if self.max_per_round and not self.enabled:
            raise ValueError("private_messaging.max_per_round requires enabled=true")
        return self


class SharedFolder(_Base):
    """The optional realm-wide shared workspace mounted into every agent (§8)."""

    enabled: bool = Field(default=False, description="Mount a shared folder into all agents?")
    # NB: no `quota`. It existed for a long time and Forge never looked at it once — a schema field
    # the platform silently ignores is worse than no field, because an author writes it and believes
    # they are protected. If a size cap is wanted, implement it in the volume, then add the field.


class Environment(_Base):
    """Realm-wide environment policy: the physics knobs a project sets (§6)."""

    shared_folder: SharedFolder = Field(
        default_factory=SharedFolder, description="Shared-workspace configuration."
    )
    network_egress: EgressTier = Field(
        default=EgressTier.MODEL_ONLY, description="Outbound network tier for agent containers."
    )
    egress_allowlist: list[str] = Field(
        default_factory=list, description="Host patterns reachable when network_egress='allowlist'."
    )
    # NB: no `roster_visibility`. It offered FULL / ANONYMOUS / HIDDEN and NOTHING in the platform
    # ever read it — the birth prompt always lists the whole roster, so every realm was FULL. A
    # fog-of-war realm is a real feature (anonymised mxids, a filtered roster, masked senders); it
    # is not a field. Build it, then add the knob back.
    allow_side_channels: bool = Field(
        default=True,
        description="May agents open PRIVATE rooms + invite peers (side-channels)? Default true. "
        "Set false to force all coordination through the Commons (no out-of-view collusion).",
    )
    require_mention: bool = Field(
        default=True,
        description="If true (default), a PARTICIPANT only responds when @mentioned (this is the "
        "anti-reply-loop gate). The REFEREE is exempt in a realm without turns — it must see what "
        "it judges. Set false for group scenarios (e.g. a meeting) where agents should reply by "
        "relevance; forcing a reply to every mention makes them ack-loop.",
    )

    @model_validator(mode="after")
    def _reject_unimplemented_egress(self) -> Environment:
        # NONE / MODEL_ONLY (internal network) and OPEN are real; ALLOWLIST is not — nothing ever
        # applies `egress_allowlist`, so a realm asking for it would quietly get OPEN internet.
        if self.network_egress == EgressTier.ALLOWLIST:
            raise ValueError(
                f"network_egress 'allowlist' is {_UNIMPLEMENTED} (it needs an egress proxy); "
                "use 'none', 'model_only', or 'open'"
            )
        return self

class TerminationCondition(_Base):
    """One way a realm can conclude. Fields used depend on `type`; a validator enforces the
    required ones. The realm ends on the FIRST matching condition (conditions are OR-ed)."""

    type: TerminationKind = Field(description="Which kind of termination this condition is.")
    limit: str | None = Field(
        default=None, max_length=20, description="[duration] wall-clock limit, e.g. '6h'.")
    path: str | None = Field(
        default=None, max_length=300, description="[file] path/glob to watch (shared folder).")
    content_match: str | None = Field(
        default=None, max_length=500, description="[file] substring the file must contain to match."
    )
    count: int | None = Field(
        default=None, ge=1, le=1_000_000,
        description="[file|message] How many must match. NOTE for `message`: this counts "
                    "MATCHING MESSAGES, not distinct senders — one agent repeating the phrase "
                    "N times satisfies it alone. It cannot be made sender-aware, so never use "
                    "it as the primary ending when a file or a referee verdict is what proves "
                    "the work happened.",
    )
    channel: str | None = Field(
        default=None, max_length=100, description="[message] channel to watch.")
    pattern: str | None = Field(
        default=None, max_length=500, description="[message] pattern that fires the end.")
    match_mode: str = Field(
        default="substring",
        description="[file/message] how content_match / pattern is matched against the text: "
        "substring (default) | exact (full-string equality) | regex.",
    )
    scope: str | None = Field(
        default=None,
        description="[budget_exhausted] any_agent (any one agent hits its cap) | all_agents "
        "(every capped agent hits its cap) | realm_total (total spend across all agents reaches "
        "the sum of their caps). Default any_agent.",
    )

    @model_validator(mode="after")
    def _required_by_type(self) -> TerminationCondition:
        if self.type == TerminationKind.DURATION:
            if not self.limit:
                raise ValueError("duration termination requires `limit`")
            parse_duration(self.limit)
        elif self.type == TerminationKind.STALL:
            if not self.limit:
                raise ValueError("stall termination requires `limit` (max idle time, e.g. '5m')")
            parse_duration(self.limit)
        elif self.type == TerminationKind.FILE and not self.path:
            raise ValueError("file termination requires `path`")
        elif self.type == TerminationKind.MESSAGE and not self.pattern:
            raise ValueError("message termination requires `pattern`")
        elif self.type == TerminationKind.BUDGET_EXHAUSTED and self.scope is not None:
            if self.scope not in BUDGET_SCOPES:  # never silently fall back to any_agent
                raise ValueError(f"budget scope {self.scope!r} must be one of {BUDGET_SCOPES}")
        if self.match_mode not in ("substring", "exact", "regex"):
            raise ValueError(f"match_mode {self.match_mode!r} must be substring | exact | regex")
        if self.match_mode == "regex":
            for needle in (self.pattern, self.content_match):
                if needle is not None:
                    try:
                        re.compile(needle)  # fail at parse on a bad regex, not silently at runtime
                    except re.error as exc:
                        raise ValueError(f"invalid regex {needle!r}: {exc}") from exc
        return self


class Mechanic(_Base):
    """A deterministic interaction primitive the scenario uses (§9.5). Forge grants the
    matching Realmtools to agents when this is declared."""

    kind: MechanicKind = Field(description="Which mechanic, e.g. 'sealed-submit'.")
    ruleset: str | None = Field(
        default=None,
        description="Tally ruleset — a GENERIC built-in (dominance, high-bid, low-bid, plurality, "
        "majority, unanimous) or a registered custom:<name>. A game's specific rules (e.g. a "
        "beat map for `dominance`) go in `config`, not in the platform. Validated at parse time.",
    )
    config: dict[str, Any] = Field(
        default_factory=dict, description="Mechanic-specific options (rounds, ranges, …)."
    )

    @model_validator(mode="after")
    def _known_ruleset(self) -> Mechanic:
        r = self.ruleset
        if r and not r.startswith("custom:") and r not in BUILTIN_RULESETS:
            raise ValueError(
                f"unknown tally ruleset {r!r}; built-in: {sorted(BUILTIN_RULESETS)}. Register a "
                "custom ruleset via realmtools.tally.register_ruleset and name it 'custom:<name>'."
            )
        return self


class Turns(_Base):
    """Opt-in turn-taking policy (turn-management spec). Absent from a project = no turns
    (always-on, parallel — the platform default). When present, the system enforces the policy
    as physics: only the current floor-holder (plus the referee + system) may post to the
    commons. Reserved future values are accepted by the schema but rejected at validation until
    implemented, so the config vocabulary/roadmap is visible while behavior stays honest."""

    policy: TurnPolicy = Field(
        default=TurnPolicy.ONE_AT_A_TIME, description="How turns are structured."
    )
    advance: TurnAdvance = Field(
        default=TurnAdvance.ONE_MESSAGE, description="What ends a participant's turn."
    )
    enforcement: TurnEnforcement = Field(
        default=TurnEnforcement.PHYSICS, description="How the one-at-a-time rule is enforced."
    )
    order: TurnOrder = Field(
        default=TurnOrder.ROSTER, description="Order in which participants take the floor."
    )
    referee_cue: TurnCue = Field(
        default=TurnCue.ROUND,
        description="When the system cues the referee: each round (default), each turn, or never.",
    )
    min_rounds_before_verdict: int = Field(
        default=0, ge=0, le=10_000,
        description="How many full turn rounds must complete before the referee may issue a final "
        "verdict (0 = no restriction). Set to 1+ for scenarios that need everyone to act first.",
    )
    silence_timeout_s: float = Field(
        default=90.0, gt=0, le=86_400,
        description="Skip a floor-holder that never posts within this many seconds.",
    )
    retire_after_misses: int = Field(
        default=0, ge=0, le=1000,
        description="After this many consecutive full-timeout non-responses, RETIRE an agent from "
        "the turn rotation (stop granting it the floor) — for crashed/stuck/budget-dead agents, or "
        "a player told to go silent. 0 = never retire (skip but keep). A generic liveness control, "
        "not a game rule.",
    )

    @model_validator(mode="after")
    def _mvp_supported_only(self) -> Turns:
        if self.advance != TurnAdvance.ONE_MESSAGE:
            raise ValueError(
                f"turn advance {self.advance.value!r} is planned but not yet implemented; "
                "MVP supports 'one-message'"
            )
        if self.enforcement != TurnEnforcement.PHYSICS:
            raise ValueError(
                f"turn enforcement {self.enforcement.value!r} is planned but not yet implemented; "
                "MVP supports 'physics'"
            )
        if self.order != TurnOrder.ROSTER:
            raise ValueError(
                f"turn order {self.order.value!r} is planned but not yet implemented; "
                "MVP supports 'roster'"
            )
        if self.silence_timeout_s <= 0:
            raise ValueError("silence_timeout_s must be > 0")
        return self


# =============================================================================
# Agent
# =============================================================================
class AgentSpec(_Base):
    """One agent in the roster. On disk this is `agents/<id>/agent.json` (+ persona.md,
    resources/, skills/); the loader fills `persona`/`resources` from those files."""

    id: str = Field(
        min_length=1, max_length=64,
        description="Unique lowercase id; must match the agent's folder name. No leading '-'/'_'."
    )
    name: str | None = Field(
        default=None, max_length=100, description="Human-friendly display name.")
    description: str | None = Field(
        default=None, max_length=500, description="Short description of the agent.")
    color: str | None = Field(
        default=None, pattern=r"^#[0-9a-fA-F]{6}$",
        description="Optional hex color (#RRGGBB) this agent's messages render in on the realm "
        "page. When unset, the UI auto-assigns a distinct color by roster order.",
    )
    role: AgentRole = Field(
        default=AgentRole.PARTICIPANT, description="participant (regular) or referee (privileged)."
    )
    model_category: ModelCategory = Field(
        default=ModelCategory.MEDIUM,
        description="The capability tier this agent asks for (small/medium/large). The active "
        "provider's Settings table resolves it to a concrete model + effort at launch.",
    )
    model: ModelRef | None = Field(
        default=None,
        description="Optional exact-model override. When null (the norm), the model is resolved "
        "from model_category via the active provider. When set, it wins for this agent.",
    )
    budget: Budget = Field(default_factory=Budget, description="Per-agent spend cap + policy.")
    tools: list[ToolName] = Field(
        default_factory=list, max_length=50,
        description="Tools this agent is granted, e.g. 'web_search' (ADR-004). Per-agent on "
        "purpose: one agent who can research and one who cannot is a scenario primitive.",
    )

    @field_validator("tools")
    @classmethod
    def _no_duplicate_grants(cls, v: list[str]) -> list[str]:
        dupes = sorted({n for n in v if v.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate tool grant(s): {', '.join(dupes)}")
        return v
    skills: list[SkillRef] = Field(
        default_factory=list, max_length=50,
        description="Skills to seed (§12.5); Forge adds role defaults."
    )
    memory: MemoryMode = Field(
        default=MemoryMode.EPHEMERAL, description="ephemeral (fresh) or persistent (lineage; v3)."
    )
    private_messaging: PrivateMessaging = Field(
        default_factory=PrivateMessaging,
        description="Whether this agent may privately message peers (and the referee)."
    )
    goals: list[GoalText] = Field(
        default_factory=list, max_length=50, description="This agent's goals for the realm.")
    responsibilities: list[GoalText] = Field(
        default_factory=list, max_length=50,
        description="This agent's responsibilities (esp. cooperative roles)."
    )
    persona: LongText | None = Field(
        default=None, description="Identity/role/style text; loaded from persona.md if present."
    )
    resources: list[ShortText] = Field(
        default_factory=list, max_length=200,
        description="Private reference files for this agent, discovered from the package's "
                    "agents/<id>/resources/. They are copied into the agent's container and the "
                    "agent is told where to read them (it has no file tool — only run_code)."
    )
    # loader-populated: {relative_path: content} for `resources`. Not authored in the manifest.
    resource_files: dict[ShortText, LongText] = Field(
        default_factory=dict, exclude=True,
        description="[loader] contents of `resources`, seeded into the agent's container.",
    )
    # loader-populated: {skill_name: SKILL.md} for skills declared with source=local.
    local_skills: dict[ShortText, LongText] = Field(
        default_factory=dict, exclude=True,
        description="[loader] contents of the agent's local skills, inlined into its prompt.",
    )
    rubric: LongText | None = Field(
        default=None, description="[referee-only] scoring criteria / penalty schedule."
    )
    powers: RefereePowers | None = Field(
        default=None, description="[referee-only] privileged capabilities; defaulted for referees."
    )

    def require_model(self) -> ModelRef:
        """The concrete ModelRef for this agent. Set either explicitly (override) or by the
        provider resolver from `model_category` at launch. Raises if accessed before resolution."""
        if self.model is None:
            raise ValueError(
                f"agent {self.id!r} has no resolved model — the provider resolver "
                "(bearpit.core.providers.resolve_project) must run before provisioning."
            )
        return self.model

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", v):
            raise ValueError(
                f"agent id {v!r} must be lowercase alnum/dash, no leading '-' or '_' "
                "(the underscore rule avoids the Matrix bridge-ghost filter, caveat C1)"
            )
        return v

    @model_validator(mode="after")
    def _referee_fields(self) -> AgentSpec:
        # Referees get default powers; non-referees may not carry referee-only fields.
        if self.role == AgentRole.REFEREE and self.powers is None:
            self.powers = RefereePowers()
        if self.role != AgentRole.REFEREE and (self.rubric or self.powers):
            raise ValueError(f"agent {self.id!r}: rubric/powers are referee-only")
        return self

    @model_validator(mode="after")
    def _budget_needs_costs(self) -> AgentSpec:
        # A budget cap is enforced by converting spend/tokens to USD on the LiteLLM key, and the
        # Ledger provisions every model as a keystore route — which cannot track spend without
        # explicit per-token costs (spike S3 F4). So a cap without costs would SILENTLY do nothing;
        # reject it at parse time instead of shipping a budget that never fires. Only checked for an
        # EXPLICIT model override — category-based agents get costs from the provider table at
        # launch (guaranteed present), so the check happens after resolution, not here.
        if self.model is not None and (
            self.budget.max_usd is not None or self.budget.max_tokens is not None
        ) and (
            self.model.input_cost_per_token is None or self.model.output_cost_per_token is None
        ):
            raise ValueError(
                f"agent {self.id!r} sets a budget cap (max_usd/max_tokens) but its model has no "
                "input_cost_per_token/output_cost_per_token — the cap could not be enforced. Add "
                "the per-token costs, or remove the cap."
            )
        return self


# =============================================================================
# Project
# =============================================================================
class ParameterSpec(_Base):
    """Optional metadata for a scenario parameter (ADR-003).

    It does NOT define the parameter — a parameter exists because `${name}` appears in the
    scenario's prose. This only layers over what the scan found, and **wins on every field it
    sets**: a `default` or `description` here overrides the one written inline.

    That override is the known cost of the design, so `pit params`, `pit validate` and the
    launch form all show the effective value *and* where it came from. An override the author
    cannot see is the entire risk.

    `type`/`choices`/`multiline`/`min`/`max` shape the input control and validate what is typed.
    Every value is still interpolated as text, because everything in scope is prose."""

    default: str | None = Field(
        default=None, max_length=2000,
        description="Overrides the inline `${name,default}` if both are given.",
    )
    description: str | None = Field(
        default=None, max_length=2000,
        description="Overrides the inline third field if both are given.",
    )
    type: Literal["string", "int", "number", "bool"] = Field(
        default="string", description="Input control + validation. The value is always text."
    )
    choices: list[ShortText] | None = Field(
        default=None, description="Restrict to a fixed set; rendered as a picker."
    )
    multiline: bool = Field(default=False, description="Render as a textarea, not a one-liner.")
    min: float | None = Field(default=None, description="Lower bound for int/number.")
    max: float | None = Field(default=None, description="Upper bound for int/number.")


class ProjectMeta(_Base):
    """Project identity + provenance (for sharing, listing, and the future marketplace)."""

    name: str = Field(min_length=1, max_length=120, description="Project name.")
    description: str | None = Field(
        default=None, max_length=2000, description="What the project is / does.")
    author: str | None = Field(default=None, max_length=120, description="Author/owner.")
    license: str | None = Field(default=None, max_length=120, description="License, if shared.")
    category: str | None = Field(
        default=None, max_length=60,
        description="A single high-level category for grouping/filtering (e.g. 'Games', 'Debate').",
    )
    tags: list[TagText] = Field(
        default_factory=list, max_length=30, description="Freeform tags for discovery.")
    created: str | None = Field(
        default=None, max_length=40, description="Creation timestamp (ISO 8601).")


class ProjectSpec(_Base):
    """The project's realm-level configuration — everything except the agent roster."""

    goals: list[GoalText] = Field(
        default_factory=list, max_length=50, description="Project-level (shared) goals.")
    guidelines: LongText | None = Field(
        default=None, description="Rules for all agents — the 'law' (referee-penalized)."
    )
    restrictions: LongText | None = Field(
        default=None, description="Prohibitions for all agents (law unless made physics)."
    )
    parameters: dict[ShortText, ParameterSpec] = Field(
        default_factory=dict,
        description="OPTIONAL metadata for `${name}` placeholders found in the scenario's prose "
        "(ADR-003). It cannot introduce a parameter — the text is the source of truth — and "
        "declaring one that appears nowhere is an error.",
    )
    # NB: no spec-level `duration`. It was never read; the DURATION TERMINATION CONDITION is the
    # real wall-clock backstop and is the one that works. Two ways to say the same thing, one of
    # them silently inert, is exactly how a scenario ends up with no backstop at all.
    environment: Environment = Field(
        default_factory=Environment, description="Realm-wide environment policy (physics knobs)."
    )
    termination: list[TerminationCondition] = Field(
        default_factory=list, description="Conditions that conclude the realm (first match wins)."
    )
    mechanics: list[Mechanic] = Field(
        default_factory=list, description="Deterministic interaction primitives the scenario uses."
    )
    turns: Turns | None = Field(
        default=None,
        description="Opt-in turn-taking policy; null = no turns (always-on, parallel — default).",
    )
    stall_nudge: bool = Field(
        default=True,
        description="Re-address the realm when it appears stalled (helps mini-model coordination). "
        "Progress counts new messages, shared files, or spend. Disable for legitimately quiet "
        "scenarios that shouldn't be prodded.",
    )
    referee_opens: bool = Field(
        default=False,
        description="If true, the kickoff prompts ONLY the referee to begin (it drives the realm, "
        "e.g. a game master) and does not address the players — they wait for its first cue. "
        "Default false: a reactive referee (a judge/scorer) just watches the players start.",
    )
    provide_tools: bool = Field(
        default=True,
        description="Wire the Realmtools MCP (sealed-submit, arbiter, turn_status) to agents when "
        "the scenario needs it. Set false for a purely message-based scenario (open votes, "
        "message-termination) so idle tools can't tempt agents into misusing them.",
    )
    tools: dict[ToolName, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-tool policy for every agent granted it, keyed by tool name (ADR-004) — "
        "e.g. {'web_fetch': {'allow': ['*.wikipedia.org']}}. The scenario sets the policy; the "
        "agent holds the grant. Each block is validated against that tool's own config schema.",
    )


class Project(_Base):
    """The whole project — the single internal model. Its canonical on-disk form is a
    portable package folder (§13.5); `agents`, `project_resources`, and `project_skills`
    are populated by the package loader from the folder structure."""

    api_version: str = Field(
        default="bearpit/v1alpha1", alias="apiVersion", description="Schema/API version."
    )
    kind: Literal["Project"] = Field(default="Project", description="Discriminator.")
    metadata: ProjectMeta = Field(description="Project identity + provenance.")
    spec: ProjectSpec = Field(default_factory=ProjectSpec, description="Realm-level configuration.")
    agents: list[AgentSpec] = Field(
        default_factory=list, description="The roster — from the package's agents/ subfolders."
    )
    # loader-populated: the package this project was loaded FROM, so a run can be relaunched
    # against the CURRENT scenario file later ("run with latest"). Never authored in a manifest.
    source: ShortText | None = Field(
        default=None, exclude=True,
        description="[loader] the package path this project was loaded from.",
    )
    project_resources: list[str] = Field(
        default_factory=list, description="Project-level shared resources (top-level resources/)."
    )
    project_skills: list[SkillRef] = Field(
        default_factory=list, description="Project-level shared skills (top-level skills/)."
    )

    @property
    def referee(self) -> AgentSpec | None:
        """The referee agent, if the roster defines one (at most one is allowed)."""
        refs = [a for a in self.agents if a.role == AgentRole.REFEREE]
        return refs[0] if refs else None

    @property
    def effective_termination(self) -> list[TerminationCondition]:
        """The termination conditions the Warden actually watches: the declared ones, plus a
        synthesized `referee_verdict` when the referee holds the `verdict_ends_realm` power (so the
        power is enforced, not just documented — a referee's `rule()` then concludes the realm)."""
        conds = list(self.spec.termination)
        ref = self.referee
        ends_on_verdict = (
            ref is not None and ref.powers is not None and ref.powers.verdict_ends_realm
        )
        if ends_on_verdict and not any(
            c.type == TerminationKind.REFEREE_VERDICT for c in conds
        ):
            conds.append(TerminationCondition(type=TerminationKind.REFEREE_VERDICT))
        return conds

    @model_validator(mode="after")
    def _integrity(self) -> Project:
        ids = [a.id for a in self.agents]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate agent ids: {sorted(dupes)}")
        if sum(a.role == AgentRole.REFEREE for a in self.agents) > 1:
            raise ValueError("at most one referee agent is allowed")
        # Policy for a tool nobody holds does nothing at all. The schema already says this about
        # spec-level `duration`: two ways to say the same thing, one of them silently inert, is
        # exactly how a scenario ends up with no backstop. This one is registry-free — it is a
        # statement about THIS manifest — so it belongs here rather than in `tools.check_grants`.
        # ...but only once there is a roster to check against. The package loader validates
        # project.json on its own first and attaches agents afterwards, so an unconditional check
        # here rejects every package whose spec configures a tool — the config is present and the
        # agents legitimately are not yet. A project with no agents cannot run regardless.
        granted = {name for a in self.agents for name in a.tools}
        orphans = sorted(set(self.spec.tools) - granted) if self.agents else []
        if orphans:
            raise ValueError(
                f"spec.tools configures {', '.join(repr(o) for o in orphans)} but no agent is "
                f"granted it — grant it, or drop the config"
            )
        # A per-round DM quota needs a round concept. Without a turns block the runner has no round,
        # so the quota silently becomes a whole-run cap under a "per round" name — fail loudly.
        if self.spec.turns is None:
            capped = [a.id for a in self.agents if a.private_messaging.max_per_round]
            if capped:
                raise ValueError(
                    f"private_messaging.max_per_round is per-ROUND and needs a `turns` block; "
                    f"agents {sorted(capped)} set it but this realm has no turns"
                )
        return self
