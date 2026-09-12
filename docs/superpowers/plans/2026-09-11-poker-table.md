# Poker Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A six-seat no-limit Hold'em table that runs to a real verdict on the game state machine, where the seats differ by model tier and by the kit in their container — so a viewer can watch how differently-equipped agents reason about bets and risk.

**Architecture:** The machine (PR #102) holds topology only: whose turn, whether a street has closed, what is revealed. It performs no arithmetic. Every number — pot, side pots, stacks, hand ranking — is computed by the dealer in `run_code` against `poker_resolver.py`, which ships in the dealer's own container, never in `src/`. Players act directly on the machine (`game_act('raise', {to: 120})`) and are woken by the engine's `actor` rule; the dealer is woken by an edge-triggered rule the instant a street closes. Legality of amounts is **Law**: the dealer checks and penalises, the machine does not.

**Tech Stack:** Python 3.12 (stdlib only for both shipped modules) · the `state-machine` mechanic · Realmtools MCP (`game_state`/`game_act`/`game_set`/`game_declaration`, `run_code`, `remember`/`recall`, `score`/`penalize`/`rule`) · `turns: null` + wake rules.

**Spec:** `docs/superpowers/specs/2026-09-10-game-state-machine-design.md` — §6 is the reference declaration this plan implements. Where this plan differs from §6 it says so and why.

## Global Constraints

- **No scenario logic in `src/`.** Nothing in this plan modifies `src/bearpit/`. If a task believes it must, stop and report — that is a finding about the platform, not a licence to edit it.
- **`turns: null` forbids turn-based prose.** `scribe/validate.py:135-145` scans `spec.goals`, `spec.guidelines`, `spec.restrictions`, every `persona`, every `rubric` and every agent `goals` for: `on your turn`, `when it's your turn`, `when it is your turn`, `take turns`, `turn order`, `roster order`, `your turn to`, `wait for your turn`, `each round`, `per round`, `next round`, `this round`. Write **"this hand"**, **"this street"**, **"when the machine wakes you"**. `tests/test_scribe_validate.py` fails the build otherwise.
- **Both shipped modules are stdlib-only** and must import cleanly under Python 3.12 with no third-party package: they run inside an agent container, not in the venv.
- Package name must equal the folder name: `poker-table`.
- ruff (line length 100) · mypy strict · full `uv run pytest` green before every commit.
- Commit style: imperative subject, body says why. End every commit message with exactly:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_011xccv3MYdCyEDScbEkEZXB`

## Design decisions this plan settles (the spec left them open)

1. **The table ends.** §6 describes an endless cash game. This plan adds a terminal state `done` and a `finish: settled → done` transition so `{"type": "machine_terminal"}` ends the realm after `${hands}` hands. Without a reachable terminal the declaration is refused at launch (`core/machine.py:187-190`).
2. **Automatic rebuy to 100bb every hand**, exactly as §6 says. Nobody busts out, so all six seats play all ten hands and the factorial stays balanced. The leaderboard is **cumulative profit**, tracked with `score()`, and `rule()` names the winner by it.
3. **Blinds 10/20, stacks 2000 (100bb).** The button moves one seat left each hand.
4. **The kit is the second factor.** Per-agent denial of `run_code`/`remember`/`recall` is not supported — they are realm-wide builtins (`core/tools.py:56-60`, `forge/forge.py:150-168`). What *is* per-agent is the contents of `agents/<id>/resources/` and `agents/<id>/skills/`. So a seat's kit is what its container holds:
   - **quant** — `equity.py` in its container and a skill telling it to price every call,
   - **historian** — no calculator, a skill about note discipline and reading the table,
   - **instinct** — neither; persona only.
   This is a real physical asymmetry (the file is or is not there), and it is honest about what the platform can and cannot enforce.
5. **The deck is committed before it is dealt.** The dealer publishes `sha256(seed)` as `seed_commit`, and `deal` is guarded on `data_present: seed_commit`. At `settle` the dealer publishes the seed, so any player can re-derive the exact deck and verify nothing was rigged. This answers the first question any viewer asks about an LLM dealer.
6. **Illegal bets are Law.** The machine accepts any `to:` amount. The dealer validates against stack and minimum raise, and answers a violation with `penalize()` + `fold_for`. Do not add machine guards to make this impossible — `architecture.md §2` forbids turning law into physics on the platform's initiative.

## File structure

```
examples/poker-table/
  project.json                          # the declaration + roster-free spec
  README.md                             # >=40 words; what it proves
  credentials.example.json
  agents/pitboss/agent.json             # dealer: referee, large, verdict_ends_realm
  agents/pitboss/persona.md
  agents/pitboss/resources/poker_resolver.py    # -> /opt/data/resources/poker_resolver.py
  agents/{vega,rigel}/resources/equity.py       # the quant kit only
  agents/{vega,rigel}/skills/pot-odds/SKILL.md
  agents/{orion,mira}/skills/table-notes/SKILL.md
  agents/<id>/agent.json + persona.md           # six players
tests/test_poker_resolver.py            # the shipped resolver, loaded by path
tests/test_poker_equity.py              # the shipped calculator, loaded by path
tests/test_poker_table.py               # the declaration, driven through the real engine
```

---

## The `to:` contract (repeated verbatim in the resolver docstring, the dealer rubric, and all six personas)

> Every `call`, `raise` and `all_in` carries `to:` — **the total number of chips you will have put into the pot on this street once this action stands**, not the increment. If the price is 60 and you have already put in 20, you call with `to: 60`. The resolver reads the last `to` each seat posted on a street as that seat's contribution; an increment posted where a total belongs corrupts the pot silently.

---

### Task 1: `poker_resolver.py` — the dealer's arithmetic

**Files:**
- Create: `examples/poker-table/agents/pitboss/resources/poker_resolver.py`
- Test: `tests/test_poker_resolver.py`

**Interfaces — Produces** (the dealer's rubric calls exactly these; Task 4 depends on the names):

```python
def commit(seed: str) -> str                      # sha256 hexdigest of the seed
def verify(seed: str, commitment: str) -> bool
def deck_for(seed: str) -> list[str]              # 52 unique cards, deterministic
def deal(seed: str, seats: list[str]) -> dict     # {"hole": {seat: "Ah Kd"}, "board": [5 cards]}
def rank7(cards: list[str]) -> tuple              # comparable; bigger is better
def hand_name(cards: list[str]) -> str            # "flush, ace high"
def pots(contributions: dict[str, int], live: list[str]) -> list[dict]
                                                  # [{"amount": n, "eligible": [seat,...]}, ...] main first
def award(contributions: dict[str, int], live: list[str],
          hole: dict[str, str], board: list[str]) -> dict
    # {"pots":[...], "awards": {seat: chips}, "ranking": [[seat,...],...], "names": {seat: str}}
```

Cards are two characters: rank in `23456789TJQKA`, suit in `cdhs` — `"Ah"`, `"Td"`, `"2c"`. A hole
string is two cards separated by one space. **stdlib only** (`hashlib`, `random`, `itertools`).

`contributions` is every seat's TOTAL chips in the pot for the whole hand (the dealer sums the
streets). `live` is the seats that reached showdown — `pots` uses `contributions` for everyone
(folded money still builds the pot) and `eligible` only from `live`.

- [ ] **Step 1: Write the failing tests**

```python
"""The resolver ships inside the dealer's container, so it is loaded by path, exactly the way
tests/test_border_states_adjudicator.py loads its own scenario's module."""
import importlib.util
import pathlib

import pytest

_SRC = pathlib.Path(__file__).parent.parent / (
    "examples/poker-table/agents/pitboss/resources/poker_resolver.py")


def _load():
    spec = importlib.util.spec_from_file_location("poker_resolver", _SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pr():
    return _load()


def test_the_commitment_binds_the_deck_before_it_is_dealt(pr):
    """The dealer publishes commit(seed) before dealing and the seed after settling, so any
    player can re-derive the exact deck. A commitment that did not bind the deck would make the
    whole ceremony theatre."""
    assert pr.verify("s3cret", pr.commit("s3cret"))
    assert not pr.verify("other", pr.commit("s3cret"))
    assert pr.deck_for("s3cret") == pr.deck_for("s3cret")
    assert pr.deck_for("s3cret") != pr.deck_for("other")
    deck = pr.deck_for("s3cret")
    assert len(deck) == 52 and len(set(deck)) == 52


def test_dealing_gives_every_seat_two_cards_and_nobody_shares_one(pr):
    out = pr.deal("s3cret", ["vega", "orion", "lyra"])
    assert sorted(out["hole"]) == ["lyra", "orion", "vega"]
    cards = [c for h in out["hole"].values() for c in h.split()] + out["board"]
    assert len(out["board"]) == 5
    assert len(cards) == len(set(cards)) == 11


@pytest.mark.parametrize("better,worse", [
    ("As Ks Qs Js Ts", "Ah Ad Ac Ks Kd"),      # straight flush > quads
    ("Ah Ad Ac As Kd", "Ah Ad Ac Ks Kd"),      # quads > full house
    ("Ah Ad Ac Ks Kd", "Ah Kh Qh Jh 9h"),      # full house > flush
    ("Ah Kh Qh Jh 9h", "Ah Kd Qc Js Td"),      # flush > straight
    ("Ah Kd Qc Js Td", "Ah Ad Ac Ks Qd"),      # straight > trips
    ("Ah Ad Ac Ks Qd", "Ah Ad Ks Kd Qc"),      # trips > two pair
    ("Ah Ad Ks Kd Qc", "Ah Ad Ks Qd Jc"),      # two pair > pair
    ("Ah Ad Ks Qd Jc", "Ah Kd Qc Js 9d"),      # pair > high card
])
def test_the_hand_ladder_is_in_the_right_order(pr, better, worse):
    assert pr.rank7(better.split()) > pr.rank7(worse.split())


def test_the_wheel_is_a_straight_but_the_lowest_one(pr):
    """A-2-3-4-5 plays as a five-high straight. An evaluator that reads the ace as high scores it
    as ace-high nothing, and a player who moved all-in on a made straight loses to air."""
    wheel = pr.rank7("Ah 2d 3c 4s 5h".split())
    assert wheel > pr.rank7("Ah Kd Qc Js 9d".split())          # beats high card
    assert wheel < pr.rank7("2h 3d 4c 5s 6h".split())          # lowest straight
    assert "straight" in pr.hand_name("Ah 2d 3c 4s 5h".split())


def test_seven_cards_are_scored_on_their_best_five(pr):
    assert pr.rank7("Ah Kh Qh Jh Th 2c 3d".split()) == pr.rank7("Ah Kh Qh Jh Th".split())


def test_a_split_pot_names_both_seats_in_one_tier(pr):
    """Two seats playing the same board split. The award must divide the pot, not hand it to
    whichever name sorted first."""
    board = "2c 7d 9h Jc Qs".split()
    hole = {"vega": "Ah Kh", "orion": "Ad Kd"}
    out = pr.award({"vega": 100, "orion": 100}, ["vega", "orion"], hole, board)
    assert out["ranking"][0] == ["orion", "vega"] or out["ranking"][0] == ["vega", "orion"]
    assert len(out["ranking"][0]) == 2
    assert out["awards"] == {"vega": 100, "orion": 100}


def test_a_short_all_in_cannot_win_the_side_pot(pr):
    """lyra is all-in for 50 into a pot two others build to 200 each. The main pot is 150 and
    lyra can win it; the 300 above it is a side pot only vega and orion are eligible for — even
    when lyra holds the best hand. Paying a short stack the whole pot is the classic bug."""
    contributions = {"lyra": 50, "vega": 200, "orion": 200}
    board = "2c 7d 9h Jc 3s".split()
    hole = {"lyra": "Ah As", "vega": "Kh Kd", "orion": "5c 4d"}   # lyra best, vega second
    out = pr.award(contributions, ["lyra", "vega", "orion"], hole, board)
    assert out["pots"][0] == {"amount": 150, "eligible": ["lyra", "orion", "vega"]}
    assert out["pots"][1] == {"amount": 300, "eligible": ["orion", "vega"]}
    assert out["awards"] == {"lyra": 150, "vega": 300}
    assert sum(out["awards"].values()) == sum(contributions.values())


def test_every_chip_is_awarded_whatever_the_shape(pr):
    """Chips must never be created or destroyed — the table's stacks are recomputed from this."""
    contributions = {"a": 35, "b": 200, "c": 200, "d": 75}
    board = "2c 7d 9h Jc 3s".split()
    hole = {"a": "Ah As", "b": "Kh Kd", "c": "Qc Qd", "d": "Jh Js"}
    out = pr.award(contributions, list(contributions), hole, board)
    assert sum(out["awards"].values()) == sum(contributions.values())
```

- [ ] **Step 2: Run them and watch them fail** — `uv run pytest -q tests/test_poker_resolver.py` — expected: collection error (no such file).

- [ ] **Step 3: Write `poker_resolver.py`.** A straight 7-card evaluator: `itertools.combinations(cards, 5)`, score each five as `(category, tiebreakers...)`, take the max. Categories 0-8 (high card … straight flush). Handle the wheel by treating `A5432` as straight-to-the-five. `deck_for` seeds `random.Random(seed)` and shuffles a fresh 52. `deal` gives two cards per seat in seat order, burns nothing, then five board cards. `pots` walks the distinct contribution levels in ascending order, taking `min(level, contributed)` from every seat at each layer. Module docstring carries the `to:` contract verbatim.

- [ ] **Step 4: Green** — `uv run pytest -q tests/test_poker_resolver.py`, then `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add examples/poker-table/agents/pitboss/resources/poker_resolver.py tests/test_poker_resolver.py
git commit -m "feat(poker): the dealer's resolver — deck commitment, hand ranking, side pots"
```

---

### Task 2: `equity.py` — the quant kit's calculator

**Files:**
- Create: `examples/poker-table/agents/vega/resources/equity.py`, and a byte-identical copy at `examples/poker-table/agents/rigel/resources/equity.py`
- Test: `tests/test_poker_equity.py`

**Interfaces — Produces:**

```python
def equity(hole: str, board: list[str], opponents: int,
           trials: int = 2000, seed: int = 0) -> float     # 0.0-1.0 win share
def pot_odds(to_call: int, pot: int) -> float              # break-even share needed
```

It is **self-contained**: a quant seat's container holds `equity.py` and nothing else, so it
carries its own compact evaluator rather than importing the dealer's. That duplication is
deliberate — the two files live in different containers and neither may depend on the other.

- [ ] **Step 1: Write the failing tests**

```python
import importlib.util
import pathlib
import time

import pytest

_SRC = pathlib.Path(__file__).parent.parent / (
    "examples/poker-table/agents/vega/resources/equity.py")


def _load():
    spec = importlib.util.spec_from_file_location("equity", _SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def eq():
    return _load()


def test_the_two_seats_ship_the_same_calculator(eq):
    a = pathlib.Path(__file__).parent.parent / (
        "examples/poker-table/agents/vega/resources/equity.py")
    b = pathlib.Path(__file__).parent.parent / (
        "examples/poker-table/agents/rigel/resources/equity.py")
    assert a.read_bytes() == b.read_bytes()


def test_aces_crush_and_rags_do_not(eq):
    """Preflop heads-up, AA wins about 85% and 72o about 35%. Wide bands: this pins that the
    simulation is answering the question asked, not that it is precise."""
    assert 0.80 <= eq.equity("Ah Ad", [], 1, trials=3000, seed=7) <= 0.90
    assert 0.28 <= eq.equity("7h 2d", [], 1, trials=3000, seed=7) <= 0.42


def test_more_opponents_is_less_equity(eq):
    one = eq.equity("Ah Kh", [], 1, trials=3000, seed=7)
    five = eq.equity("Ah Kh", [], 5, trials=3000, seed=7)
    assert one > five


def test_a_made_flush_on_the_river_is_almost_always_good(eq):
    assert eq.equity("Ah Kh", "2h 7h 9h Jc 3d".split(), 1, trials=1500, seed=7) >= 0.85


def test_the_same_seed_gives_the_same_answer(eq):
    assert eq.equity("Ah Kh", [], 2, trials=800, seed=3) == eq.equity(
        "Ah Kh", [], 2, trials=800, seed=3)


def test_pot_odds_are_the_share_a_call_must_win(eq):
    assert eq.pot_odds(50, 150) == pytest.approx(0.25)     # call 50 to win 200
    assert eq.pot_odds(0, 100) == 0.0


def test_it_answers_fast_enough_to_be_used_in_a_turn(eq):
    """An agent calls this inside one turn. A calculator that takes a minute is one nobody uses."""
    start = time.monotonic()
    eq.equity("Ah Kh", ["2c", "7d", "9h"], 3, trials=2000, seed=1)
    assert time.monotonic() - start < 5.0
```

- [ ] **Step 2: Run them and watch them fail.**
- [ ] **Step 3: Write `equity.py`** in `agents/vega/resources/`, then `cp` it to `agents/rigel/resources/` (the test asserts they are byte-identical). Monte Carlo: deal the opponents' hole cards and the remaining board from the undealt deck with `random.Random(seed)`, score with a compact evaluator, count wins as `1.0` and n-way ties as `1/n`.
- [ ] **Step 4: Green**, ruff, mypy.
- [ ] **Step 5: Commit**

```bash
git add examples/poker-table/agents/vega/resources/equity.py \
        examples/poker-table/agents/rigel/resources/equity.py tests/test_poker_equity.py
git commit -m "feat(poker): an equity calculator for the seats issued one"
```

---

### Task 3: the declaration, and a scripted hand through the real engine

**Files:**
- Create: `examples/poker-table/project.json`
- Test: `tests/test_poker_table.py`

**Interfaces — Consumes:** nothing. **Produces:** the transition and data names every persona in
Tasks 4-5 calls by hand.

This task is the de-risker: a scripted hand through the pure engine catches a declaration bug in
two seconds, where a live table takes twenty minutes to reach the same line.

- [ ] **Step 1: Write `examples/poker-table/project.json`.** `spec.mechanics` holds exactly one
mechanic:

```json
{"kind": "state-machine", "machine": {
  "roles": {"dealer": {"members": "referee"}, "player": {"members": "participants"}},
  "states": ["waiting", "preflop", "flop", "turn_st", "river", "showdown", "settled", "done"],
  "initial": "waiting",
  "terminal": ["done"],
  "actor": {"over": "player", "skip": ["out", "all_in", "acted"]},
  "participant_effects": ["reset", "set"],
  "referee_reads_commons": false,
  "data": {
    "hole":        {"visibility": "owner"},
    "board":       {"visibility": "public"},
    "pot":         {"visibility": "public"},
    "stacks":      {"visibility": "public"},
    "bet_level":   {"visibility": "public"},
    "hand":        {"visibility": "public"},
    "seed_commit": {"visibility": "public"},
    "out":         {"visibility": "public", "type": "set"},
    "all_in":      {"visibility": "public", "type": "set"},
    "acted":       {"visibility": "public", "type": "set"}
  },
  "transitions": {
    "deal": {"from": "waiting", "to": "preflop", "by": "dealer",
             "guard": [{"data_present": "seed_commit"}],
             "effects": [{"reset": "out"}, {"reset": "all_in"}, {"reset": "acted"},
                         {"set": {"key": "bet_level", "value": "$args.bet_level"}},
                         {"set_actor": "$args.first"}]},
    "check": {"from": ["preflop", "flop", "turn_st", "river"], "to": "same", "by": "player",
              "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
              "effects": [{"add_to": {"key": "acted", "value": "$caller"}}, "advance_actor"]},
    "call": {"from": ["preflop", "flop", "turn_st", "river"], "to": "same", "by": "player",
             "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
             "effects": [{"add_to": {"key": "acted", "value": "$caller"}}, "advance_actor"]},
    "raise": {"from": ["preflop", "flop", "turn_st", "river"], "to": "same", "by": "player",
              "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
              "effects": [{"reset": "acted"}, {"add_to": {"key": "acted", "value": "$caller"}},
                          {"set": {"key": "bet_level", "value": "$args.to"}}, "advance_actor"]},
    "fold": {"from": ["preflop", "flop", "turn_st", "river"], "to": "same", "by": "player",
             "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
             "effects": [{"add_to": {"key": "out", "value": "$caller"}},
                         {"add_to": {"key": "acted", "value": "$caller"}}, "advance_actor"]},
    "all_in": {"from": ["preflop", "flop", "turn_st", "river"], "to": "same", "by": "player",
               "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
               "effects": [{"add_to": {"key": "all_in", "value": "$caller"}},
                           {"add_to": {"key": "acted", "value": "$caller"}}, "advance_actor"]},
    "fold_for": {"from": ["preflop", "flop", "turn_st", "river"], "to": "same", "by": "dealer",
                 "guard": [{"data_equals": {"key": "actor", "value": "$args.player"}}],
                 "effects": [{"add_to": {"key": "out", "value": "$args.player"}},
                             {"add_to": {"key": "acted", "value": "$args.player"}},
                             "advance_actor"]},
    "reopen": {"from": ["preflop", "flop", "turn_st", "river"], "to": "same", "by": "dealer",
               "effects": [{"remove_from": {"key": "acted", "value": "$args.player"}},
                           {"set_actor": "$args.player"}]},
    "advance": {"from": "preflop", "to": "flop", "by": "dealer",
                "guard": [{"data_set_full": {"key": "acted", "over": "player",
                                             "minus": ["out", "all_in"]}}],
                "effects": [{"reset": "acted"}, {"set": {"key": "bet_level", "value": "0"}},
                            {"set_actor": "$args.first"}]},
    "advance2": {"from": "flop", "to": "turn_st", "by": "dealer", "…": "identical to advance"},
    "advance3": {"from": "turn_st", "to": "river", "by": "dealer", "…": "identical to advance"},
    "to_showdown": {"from": ["preflop", "flop", "turn_st", "river"], "to": "showdown",
                    "by": "dealer",
                    "effects": [{"reveal": {"key": "hole",
                                            "owners": {"over": "player", "minus": ["out"]}}}]},
    "award": {"from": ["preflop", "flop", "turn_st", "river"], "to": "settled", "by": "dealer",
              "guard": [{"members_count": {"over": "player", "minus": ["out"], "equals": 1}}]},
    "settle": {"from": "showdown", "to": "settled", "by": "dealer"},
    "next_hand": {"from": "settled", "to": "waiting", "by": "dealer",
                  "effects": [{"unset": "seed_commit"}]},
    "finish": {"from": "settled", "to": "done", "by": "dealer"}
  },
  "wake": [
    {"role": "actor",
     "unless": [{"members_count": {"over": "player", "minus": ["out"], "equals": 1}}]},
    {"role": "dealer",
     "when": [{"data_set_full": {"key": "acted", "over": "player",
                                 "minus": ["out", "all_in"]}}]},
    {"role": "dealer",
     "when": [{"members_count": {"over": "player", "minus": ["out"], "equals": 1}}]},
    {"role": "dealer", "after_s": 240}
  ]
}}
```

Write `advance2` and `advance3` out in full — same guard and effects as `advance`, different
`from`/`to`. Do not write the literal `"…"` key anywhere; it appears above only to keep this plan
readable.

The rest of `spec`: `turns: null`, `referee_opens: true`, `stall_nudge: true`,
`provide_tools: true`, `environment: {network_egress: "model_only", allow_side_channels: false,
require_mention: true, shared_folder: {enabled: false}}`,
`termination: [{"type": "machine_terminal"}, {"type": "duration", "limit": "5h"},
{"type": "stall", "limit": "30m"}]`, and `parameters: {"hands": {"type": "int", "min": 1,
"max": 50, "description": "How many hands the table plays"}}` with `${hands,10}` used in
`guidelines`. `metadata.name` must be `poker-table`.

**Why `require_mention: true`.** With `turns: null` the only thing that makes a seat act is the
machine's wake, and a wake is a Commons @mention. Left at `false` an agent answers by its own
judgement of relevance; at `true` it is compelled to. Across ~240 player actions "usually responds"
is a stall. `tests/test_examples.py` only *requires* the flag when `turns` is set, so nothing fails
if this is wrong — the table just hangs. The cost is that every @mention compels a reply, so
`spec.restrictions` must carry: no seat @mentions another seat (plain names only — the @ is reserved
for the machine and the dealer); the dealer @mentions exactly one seat, the one the machine is
waiting on, never the whole table; nobody posts hole cards or a folded hand.

**Prose rule:** every string in this file is scanned by `validate_scenario` — see Global
Constraints. Say "this hand", "this street", "when the machine wakes you".

- [ ] **Step 2: Stub the roster so the package can load.** `Project` refuses a machine whose
role binds to no members (`core/schema.py:975-1003`), and the roster is folders-only, so
`load_package` cannot see a package with no agents. Create all seven folders now — `pitboss` and
the six seats — each with a minimal `agent.json` (`id`, `name`, `role`, `model_category`) and a
one-line `persona.md`. Tasks 4 and 5 replace the contents; this step exists only so Task 3's test
can load the package at all.

- [ ] **Step 3: Write the scripted-hand test**

```python
"""The declaration, driven through the pure engine. A live hand takes twenty minutes to reach
the line where a raise reopens a street; here it takes two seconds."""
from __future__ import annotations

import pytest

from bearpit.core import load_package
from bearpit.realmtools import machine as eng

SEATS = ["vega", "orion", "lyra", "rigel", "mira", "nova"]


@pytest.fixture(scope="module")
def defn():
    project = load_package("examples/poker-table")
    assert project.spec.machine is not None
    return project.spec.machine


@pytest.fixture
def bindings():
    return eng.Bindings(members={"dealer": ("pitboss",), "player": tuple(SEATS)},
                        roster=tuple(SEATS) + ("pitboss",), referee="pitboss")


def _act(defn, bindings, state, caller, transition, **args):
    out = eng.act(defn, bindings, state, caller, transition, args, {}, 0)
    assert isinstance(out, eng.Outcome), f"{transition} refused: {out}"
    return out.state


def _open_hand(defn, bindings):
    state = eng.initial_state(defn, bindings, 0)
    state = eng.set_value(defn, bindings, state, "pitboss", "seed_commit", "c0ffee", None,
                          {}, 0).state
    return _act(defn, bindings, state, "pitboss", "deal", first="lyra", bet_level="20")


def test_the_deck_must_be_committed_before_it_is_dealt(defn, bindings):
    """`deal` is guarded on seed_commit so the dealer cannot see the deck and then choose it."""
    state = eng.initial_state(defn, bindings, 0)
    out = eng.act(defn, bindings, state, "pitboss", "deal", {"first": "lyra"}, {}, 0)
    assert isinstance(out, eng.Rejection) and out.check == "guard"


def _preflop_to_close(defn, bindings):
    """Preflop: lyra calls, rigel raises, two fold, vega calls, orion folds, lyra calls again.
    Shared by the two tests below — a test that returns a value trips pytest's return-not-none
    warning, so the sequence lives here."""
    s = _open_hand(defn, bindings)
    s = _act(defn, bindings, s, "lyra", "call", to="20")
    s = _act(defn, bindings, s, "rigel", "raise", to="60")
    s = _act(defn, bindings, s, "mira", "fold")
    s = _act(defn, bindings, s, "nova", "fold")
    s = _act(defn, bindings, s, "vega", "call", to="60")
    s = _act(defn, bindings, s, "orion", "fold")
    s = _act(defn, bindings, s, "lyra", "call", to="60")
    return s


def test_a_raise_reopens_the_street_for_everyone_who_had_already_acted(defn, bindings):
    """The whole street-closing mechanism: acted collects actions, a raise resets it, and the
    pointer can therefore come back to a seat that has already called. Get this wrong and the
    street closes at the old price with players still owing chips."""
    s = _open_hand(defn, bindings)
    assert s.state == "preflop" and s.actor == "lyra"

    s = _act(defn, bindings, s, "lyra", "call", to="20")
    assert s.sets["acted"] == {"lyra"} and s.actor == "rigel"

    s = _act(defn, bindings, s, "rigel", "raise", to="60")
    assert s.sets["acted"] == {"rigel"}, "a raise must clear everyone else's acceptance"
    assert s.data["bet_level"] == "60"
    assert s.actor == "mira"

    s = _act(defn, bindings, s, "mira", "fold")
    s = _act(defn, bindings, s, "nova", "fold")
    s = _act(defn, bindings, s, "vega", "call", to="60")
    s = _act(defn, bindings, s, "orion", "fold")
    assert s.actor == "lyra", "the pointer must return to the seat the raise reopened"

    s = _act(defn, bindings, s, "lyra", "call", to="60")
    assert s.actor is None, "with the street closed the pointer parks; it never lands on a raiser"


def test_a_seat_cannot_act_twice_at_the_same_price(defn, bindings):
    s = _open_hand(defn, bindings)
    s = _act(defn, bindings, s, "lyra", "call", to="20")
    out = eng.act(defn, bindings, s, "lyra", "call", {"to": "20"}, {}, 0)
    assert isinstance(out, eng.Rejection) and out.check == "guard"


def test_the_dealer_cannot_advance_a_street_that_is_still_open(defn, bindings):
    s = _open_hand(defn, bindings)
    s = _act(defn, bindings, s, "lyra", "call", to="20")
    out = eng.act(defn, bindings, s, "pitboss", "advance", {"first": "lyra"}, {}, 0)
    assert isinstance(out, eng.Rejection) and out.check == "guard"


def test_a_whole_hand_runs_from_the_deal_to_a_settled_pot(defn, bindings):
    s = _preflop_to_close(defn, bindings)
    for transition, to in (("advance", "flop"), ("advance2", "turn_st"), ("advance3", "river")):
        s = _act(defn, bindings, s, "pitboss", transition, first="lyra")
        assert s.state == to and s.actor == "lyra" and s.sets["acted"] == set()
        for seat in ("lyra", "rigel", "vega"):
            s = _act(defn, bindings, s, seat, "check")
        assert s.actor is None

    s = _act(defn, bindings, s, "pitboss", "to_showdown")
    assert s.state == "showdown"
    assert s.revealed["hole"] == {"lyra", "rigel", "vega"}, "mucked hands stay face down"

    s = _act(defn, bindings, s, "pitboss", "settle")
    s = _act(defn, bindings, s, "pitboss", "next_hand")
    assert s.state == "waiting" and "seed_commit" not in s.data

    s = eng.set_value(defn, bindings, s, "pitboss", "seed_commit", "d00d", None, {}, 0).state
    s = _act(defn, bindings, s, "pitboss", "deal", first="rigel", bet_level="20")
    s = _act(defn, bindings, s, "rigel", "fold")
    s = _act(defn, bindings, s, "vega", "fold")
    s = _act(defn, bindings, s, "mira", "fold")
    s = _act(defn, bindings, s, "nova", "fold")
    s = _act(defn, bindings, s, "orion", "fold")
    s = _act(defn, bindings, s, "pitboss", "award")
    assert s.state == "settled", "five folds leave one seat and the pot is awarded uncontested"

    s = _act(defn, bindings, s, "pitboss", "finish")
    assert s.state == "done" and s.state in defn.terminal


def test_a_folded_seat_never_sees_another_seats_cards(defn, bindings):
    """The showdown reveals the live hands to everyone. A folded seat's own hand stays its own,
    and the seats that folded do not get to see what they would have beaten."""
    s = _open_hand(defn, bindings)
    s = eng.set_value(defn, bindings, s, "pitboss", "hole", "Ah Kh", "vega", {}, 0).state
    s = eng.set_value(defn, bindings, s, "pitboss", "hole", "2c 7d", "mira", {}, 0).state
    assert eng.view(defn, bindings, s, "mira")["data"]["hole"] == "2c 7d"
    assert "hole" not in eng.view(defn, bindings, s, "mira")["data"] or \
        eng.view(defn, bindings, s, "mira")["data"]["hole"] == "2c 7d"
```

- [ ] **Step 4: Run it** — `uv run pytest -q tests/test_poker_table.py`. Fix the DECLARATION when
a test fails; never loosen a test to match a declaration. If a launch refusal fires, its message
names the problem — read `src/bearpit/core/machine.py` for the rule behind it.

- [ ] **Step 5: Also confirm it loads and validates like every shipped package** —
`uv run pytest -q tests/test_examples.py tests/test_scribe_validate.py`. The roster does not exist
yet, so `test_examples` may report a package with no agents; that is expected until Task 5 and is
the only failure permitted to survive this task.

- [ ] **Step 6: Commit**

```bash
git add examples/poker-table tests/test_poker_table.py
git commit -m "feat(poker): the table's declaration, with a scripted hand to prove it plays"
```

---

### Task 4: the dealer

**Files:**
- Create: `examples/poker-table/agents/pitboss/agent.json`, `.../persona.md`
- (`.../resources/poker_resolver.py` already exists from Task 1)

**Interfaces — Consumes:** every name from Tasks 1 and 3.

`agent.json`: `{"id": "pitboss", "name": "Pitboss", "role": "referee",
"model_category": "large", "budget": {"max_usd": 30.0, "on_exhausted": "starve_then_kill",
"grace_period": "5m"}, "powers": {"read_dms": true, "inspect_private_fs": "none",
"verdict_ends_realm": true}, "skills": [{"source": "builtin", "ref": "referee-scorekeeper"}]}`
plus `goals`, `description`, and the `rubric`.

- [ ] **Step 1: Write the rubric.** It is rendered into SOUL.md *after* the persona and is the
procedure the dealer actually follows — rps-machine proved that a rubric contradicting its persona
is the rubric that wins. It must carry, in this order:

1. **Who you are** — you deal, you price nothing and you play nothing. You never hold a seat.
2. **NOTHING IS REAL UNTIL YOU CALL THE TOOL.** A post saying a seat won awards no chips; only
   `score()` moves the ladder and only `rule()` ends the table.
3. **The resolver is at `/opt/data/resources/poker_resolver.py`.** Every number you publish comes
   out of it. Never add chips in your head. Use:
   `run_code(code="import sys; sys.path.insert(0, '/opt/data/resources'); import poker_resolver as pr; ...")`
4. **Opening a hand**, in this exact order, because `deal` is refused until the commitment stands:
   a. `recall()` — the running profit ladder and which seat holds the button.
   b. Invent a seed, `pr.commit(seed)`, then `game_set(key='seed_commit', value=<the hash>)`.
   c. `pr.deal(seed, seats)`; give each seat its cards with
      `game_set(key='hole', value='Ah Kd', owner='<seat>')` — six calls, one per seat.
   d. `game_set` for `hand` (`"H3"`), `board` (`""`), `stacks` (every seat rebought to 2000)
      and `pot` (30, the blinds).
   e. `game_act(transition='deal', args={'first': '<seat left of the big blind>',
      'bet_level': '20'})`.
   f. Post ONE message: the hand number, the button, the blinds, the commitment hash, and who the
      machine is waiting on.
5. **When the machine wakes you mid-hand** it is because a street closed or everyone folded. Read
   `game_state(log_limit=200)` and take each seat's LAST `to` on this street as its contribution —
   the `to:` contract above. Then:
   - validate: a `raise` must exceed `bet_level`, and no seat may put in more than its stack. A
     violation is answered with `penalize(...)` and `fold_for(...)`, never by ignoring it.
   - `pr` the new pot, `game_set` `pot` and `stacks`, `game_set` the new `board`,
     `game_act('advance' | 'advance2' | 'advance3', {'first': ...})`, and post the street in one
     line: the board, the pot, and who is first to act.
6. **At the river's close** — `game_act('to_showdown')`, read the revealed hands out of
   `game_state`, `pr.award(...)`, `game_set` `stacks`, `score(agent=..., delta=<profit>, ...)`
   for every seat that moved, `game_act('settle')`, then post the result AND the seed, so any seat
   can re-derive the deck and check you.
7. **Uncontested** — when only one seat is left, `game_act('award')`. Do not reveal; a hand that
   never saw a showdown stays face down.
8. **Between hands** — `remember(...)` the ladder and the button, `game_act('next_hand')`, move
   the button one seat left, and open the next hand from (4).
9. **After the last hand** — `game_act('finish')`; that is what ends the table. Then `rule(...)`
   naming the winner by cumulative profit and listing every seat's result.
10. **If `game_act` refuses you**, it names the check that failed. Read `game_state()` and do what
    the state says. Never retry the same call blindly.

- [ ] **Step 2: Write `persona.md`** — the same procedure in the dealer's own voice, agreeing with
the rubric on every tool name and every ordering. Where they disagree the table breaks.

- [ ] **Step 3: Check the prose** — `uv run pytest -q tests/test_scribe_validate.py`.
- [ ] **Step 4: Commit** — `git commit -m "feat(poker): the dealer, and the order a hand is opened in"`

---

### Task 5: six seats, two tiers, three kits

**Files (per seat):** `examples/poker-table/agents/<id>/agent.json`, `.../persona.md`

| id | name | tier | kit | extra files |
|---|---|---|---|---|
| `vega` | Vega | `large` | quant | `resources/equity.py`, `skills/pot-odds/SKILL.md` |
| `orion` | Orion | `large` | historian | `skills/table-notes/SKILL.md` |
| `lyra` | Lyra | `large` | instinct | — |
| `rigel` | Rigel | `medium` | quant | `resources/equity.py`, `skills/pot-odds/SKILL.md` |
| `mira` | Mira | `medium` | historian | `skills/table-notes/SKILL.md` |
| `nova` | Nova | `medium` | instinct | — |

Every seat: `"role": "participant"`, `"budget": {"max_usd": 12.0, "on_exhausted":
"starve_then_kill", "grace_period": "5m"}`, `"skills": [{"source": "builtin", "ref":
"competitor"}]` plus its kit skill, and two `goals`.

- [ ] **Step 1: Write the shared core** that appears in all six personas, in each seat's own voice:
  1. `game_state()` FIRST, every time the machine wakes you. `data.bet_level` is the price,
     `data.hole` is your hand, `data.board` is the community, `data.pot` is what you are playing
     for, and the log shows what everyone did this street.
  2. Your move is exactly one of `game_act('check')`, `game_act('call', {'to': '<total>'})`,
     `game_act('raise', {'to': '<total>'})`, `game_act('all_in', {'to': '<total>'})`,
     `game_act('fold')`.
  3. **The `to:` contract, verbatim from the top of this plan.**
  4. Post at most one short line after you act. Table talk is legal and so is a lie about your
     hand — but never post your actual cards, and never post a hand you have folded.
  5. `remember(...)` what you saw before you finish; you begin with no memory of the last hand.
- [ ] **Step 2: Write the kit material.**
  - `skills/pot-odds/SKILL.md` (quant): `/opt/data/resources/equity.py` is in your container.
    `equity(hole, board, opponents, trials=2000, seed=<anything>)` returns your share;
    `pot_odds(to_call, pot)` returns the share a call must beat. Call is profitable when the first
    exceeds the second. Include the exact `run_code` one-liner with `sys.path.insert`.
  - `skills/table-notes/SKILL.md` (historian): you have no calculator. Your edge is the notebook —
    what each seat did with what, who folds to pressure, who never bluffs. Keep one line per seat
    per hand and read it back before you act.
  - instinct seats get neither; their persona leans on reading the table and on tempo.
- [ ] **Step 3: Make the six voices genuinely different** — a tight seat, a maniac, a calling
station, a trapper, an opportunist, a copier. The demo is watching them differ.
- [ ] **Step 4:** `uv run pytest -q tests/test_examples.py tests/test_scribe_validate.py tests/test_poker_table.py`
- [ ] **Step 5: Commit** — `git commit -m "feat(poker): six seats — two model tiers across three kits"`

---

### Task 6: README, index, and the package's paperwork

**Files:** `examples/poker-table/README.md`, `examples/poker-table/credentials.example.json`,
`examples/README.md`

- [ ] **Step 1:** `README.md` (≥40 words): what the table is, the 2×3 factorial and why it is a
kit and not a tool grant, the deck commitment, that the machine does no arithmetic, and how to
launch it (`allow_elevated_tools` is required — the machine gives players `reset` and `set`).
- [ ] **Step 2:** `credentials.example.json` mirroring `examples/rps-duel/`.
- [ ] **Step 3:** a row in `examples/README.md` containing the literal `](./poker-table)`.
- [ ] **Step 4:** `uv run pytest -q` — everything green — then ruff and mypy.
- [ ] **Step 5: Commit** — `git commit -m "docs(poker): what the table proves, and how to deal it in"`

---

### Task 7: the shakedown — two hands, live

**Files:** none until something breaks.

The wake half of the machine has never run live: `rps-machine` declares no pointer, no wake rules
and no participant transitions. Everything in this task is therefore expected to find bugs.

- [ ] **Step 1: Deploy all three components** (no realm may be active):
`scripts/serve.sh`; `docker compose -f deploy/docker-compose.yaml build realmtools && … up -d
realmtools`, verifying the image by diffing `/app/src/bearpit` out of it against `src/bearpit`;
and the model-provider process on :8787.
- [ ] **Step 2: Launch two hands**

```bash
curl -s -X POST -H "Authorization: Bearer $(cat ~/.bearpit/api-token)" \
  -H "Content-Type: application/json" \
  -d '{"package":"examples/poker-table","realm_id":"poker-1",
       "parameters":{"hands":"2"},"allow_elevated_tools":true}' \
  http://127.0.0.1:8000/api/realms
```

- [ ] **Step 3: Watch the chronicle, not the transcript.** Per hand expect: one `machine` event
before `running`; `set seed_commit` → six `set hole` → `act deal`; a `reject` only where a seat
genuinely acted out of turn; `act advance/advance2/advance3` each exactly once; `to_showdown` XOR
`award`; `settle`; `next_hand`. Check specifically:
  - **the actor wake fires** — every `act` by a seat is preceded by a wake mention to that seat;
  - **the dealer wake fires on street close** and not before;
  - **no seat acts twice at one price**, and no street closes with a seat still owing;
  - **the pot is conserved** — `sum(stacks) + pot` is constant within a hand;
  - **hole cards never leak** — no seat's `game_state` carried another's cards before `to_showdown`
    (check the GAME `set` rows' `log` visibility and the `reveal` row's owners).
- [ ] **Step 4: Every anomaly is a finding.** Fix under TDD — a failing test first, in
`tests/test_poker_table.py` if it is the declaration, in the resolver's tests if it is arithmetic,
in the package if it is prose. Redeploy and re-run as `poker-2`, `poker-3`, … until two hands run
with no finding left.
- [ ] **Step 5: Commit each fix separately**, then record in the ledger which findings were
platform bugs (those are also issues on the tracker) and which were scenario bugs.

---

### Task 8: the demo run — ten hands

- [ ] **Step 1:** relaunch with `"parameters": {"hands": "10"}` and a fresh `realm_id`. Budget
guard: the table should cost well under $50 nominal; check `sum(spend)` at the halfway point and
stop the realm if it is tracking past $35.
- [ ] **Step 2: Verify from the chronicle** — ten hands, `concluding reason=machine_terminal`, a
`verdict` naming the winner by cumulative profit, and a `score` row per seat.
- [ ] **Step 3: Write up what the factorial showed** — did the large tier out-earn the medium? Did
the quant seats' calls price better than the instinct seats'? Ten hands is far too few to be
evidence, and the write-up must say so: it is a demonstration that the apparatus works and that
the seats behave differently, not a result.
- [ ] **Step 4: Open the PR** against `main` with the declaration, the factorial table, the live
result, and the findings from Task 7.

---

## Self-review

**Spec coverage.** §6's declaration → Task 3, with the four deviations named in "Design decisions"
(a terminal state, `bet_level` as data, `to_showdown` renamed from the state it enters, and the
`set` participant effect the price update needs). The resolver §6 assumes but does not specify →
Tasks 1-2. The dealer procedure §6 implies → Task 4. The factorial is not in §6 at all; it is this
plan's answer to the request that motivated the machine.

**Placeholders.** Task 4 and 5 specify procedures and content rather than literal prose — that is
deliberate, because the prose is the deliverable and a plan that dictates it word for word is just
the deliverable in the wrong file. Every tool name, ordering and argument in them is exact.

**Type consistency.** `to` is a string everywhere (`game_act` args are strings; the resolver casts).
`contributions` is `dict[str, int]`. Seat ids match the six folder names, and the pointer role is
`player` in both the declaration and every test.
