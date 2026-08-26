"""The shipped example packages must always load + validate (guards against schema drift)."""

from pathlib import Path

import pytest

from bearpit.core import load_package

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
PACKAGES = sorted(p.name for p in EXAMPLES.iterdir() if (p / "project.json").exists())


def test_examples_present():
    # the core three plus the scenario sweep; every package must load (parametrized below)
    assert {"beacon-brief", "market-scan-duel", "rps-duel"} <= set(PACKAGES)
    assert len(PACKAGES) >= 10


@pytest.mark.parametrize("name", PACKAGES)
def test_every_package_documents_itself(name: str):
    """A package with no README is a scenario nobody outside this repo can use. border-states —
    the most sophisticated one here, with a real deterministic adjudicator — shipped without one
    for exactly as long as nothing checked."""
    readme = EXAMPLES / name / "README.md"
    assert readme.is_file(), f"examples/{name} has no README.md"
    assert len(readme.read_text().split()) >= 40, f"examples/{name}/README.md is a stub"


@pytest.mark.parametrize("name", PACKAGES)
def test_the_index_lists_every_package(name: str):
    """examples/README.md is the index. It once described 3 of 17."""
    assert f"](./{name})" in (EXAMPLES / "README.md").read_text(), f"{name} missing from the index"


@pytest.mark.parametrize("name", PACKAGES)
def test_example_package_loads(name: str):
    project = load_package(EXAMPLES / name)
    assert project.metadata.name == name
    assert len(project.agents) >= 2
    # personas were discovered from agents/<id>/persona.md
    assert all(a.persona for a in project.agents)
    # agents reference a capability tier; the concrete model is resolved by the active provider
    for agent in project.agents:
        assert agent.model_category in ("small", "medium", "large")
        # an explicit override (rare) must still use a handle, not an embedded secret
        if agent.model is not None:
            assert not agent.model.api_key_ref.lower().startswith(("sk-", "akia"))


def test_rps_duel_has_referee_and_mechanic():
    project = load_package(EXAMPLES / "rps-duel")
    assert project.referee is not None and project.referee.id == "themis"
    assert [m.kind for m in project.spec.mechanics] == ["sealed-submit"]


def test_beacon_brief_is_cooperative_no_referee():
    project = load_package(EXAMPLES / "beacon-brief")
    assert project.referee is None
    assert project.spec.environment.shared_folder.enabled is True


def test_cygnus_crew_ends_on_referee_verdict_tool_not_message_parsing():
    # The game master ends the realm by CALLING the generic `rule` verdict tool (deterministic),
    # not by hoping a parsed "GAME OVER" message fires. Guards against reverting to the fragile
    # message-only ending that let a decided game drag on for rounds (run among-us-0b8cc0).
    import re

    from bearpit.core.schema import TerminationKind

    project = load_package(EXAMPLES / "cygnus-crew")
    ref = project.referee
    assert ref is not None and ref.id == "mother"
    assert ref.powers is not None and ref.powers.verdict_ends_realm is True
    # so a referee_verdict termination is synthesized and active
    assert TerminationKind.REFEREE_VERDICT in {c.type for c in project.effective_termination}
    # the "🏁 GAME OVER" message match survives only as a hardened fallback: emoji-anchored so a
    # player can't trip it, case-insensitive so the host's phrasing ("Game over") still matches
    msg = next(c for c in project.spec.termination if c.type == TerminationKind.MESSAGE)
    assert msg.match_mode == "regex" and msg.pattern is not None
    assert re.search(msg.pattern, "🏁 GAME OVER — Crew wins")
    assert not re.search(msg.pattern, "the game over there")  # no bare-text false trigger


def test_toolcheck_is_a_complete_platform_diagnostic():
    # the diagnostic realm must exercise every mechanism the games depend on: driving referee
    # (opener + round pause), verdict-tool termination, turns, and a tools-required flow.
    from bearpit.core.schema import TerminationKind

    project = load_package(EXAMPLES / "toolcheck")
    ref = project.referee
    assert ref is not None and ref.id == "umpire" and ref.rubric
    assert ref.powers is not None and ref.powers.verdict_ends_realm is True
    assert TerminationKind.REFEREE_VERDICT in {c.type for c in project.effective_termination}
    assert project.spec.referee_opens and project.spec.provide_tools
    turns = project.spec.turns
    assert turns is not None and turns.min_rounds_before_verdict == 1
    # the referee's full state-changing chain is named in the rubric; the participant tool in the
    # guidelines. (No `scoreboard`: an idle read-only tool just invites the referee to wander —
    # toolcheck-k1 looped on reveal/reveal_status instead of resolving.)
    for tool in ("reveal", "score", "eliminate", "rule"):
        assert tool in ref.rubric
    # ONE round with ONE literal label: the round-number coupling between submit_sealed and reveal
    # is what broke k1 (players sealed under '0'/'1', so reveal came back empty and the umpire hung)
    assert "'R1'" in ref.rubric and "R1" in (project.spec.guidelines or "")
    assert "submit_sealed" in (project.spec.guidelines or "")
    # A DIAGNOSTIC must isolate PLATFORM bugs, not model flakiness: the small tier skips required
    # tool calls non-deterministically (toolcheck-k1 sealed, k3 didn't), which would make a red run
    # ambiguous. Every agent here runs on a tier that reliably calls tools.
    assert all(a.model_category in ("medium", "large") for a in project.agents)


def test_the_invariant_count_in_the_docs_matches_the_contract() -> None:
    """Three places quote the number of invariants, and adding #19 left all three saying 18.

    A count in prose rots the moment someone adds a rule, and a reader who trusts it stops at the
    wrong place. Cheaper to assert than to remember."""
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    contract = (repo / "docs" / "scenario-contract.md").read_text()
    actual = len(re.findall(r"^## \d+\.", contract, re.M))
    for name in ("CLAUDE.md", "README.md"):
        text = (repo / name).read_text()
        for quoted in re.findall(r"(\d+) invariants", text):
            assert int(quoted) == actual, (
                f"{name} says {quoted} invariants; docs/scenario-contract.md has {actual}"
            )


@pytest.mark.parametrize("name", PACKAGES)
def test_a_placeholder_is_not_spliced_between_a_determiner_and_its_noun(name: str) -> None:
    """`a crisp ${system}` + a default of `a URL shortener` renders "a crisp a URL shortener".

    Checking the RENDERED text for two adjacent articles does not find this — an adjective sits
    between them — and loosening the regex to skip words then flags "the design for a rate
    limiter", which is correct English. So this inspects the TEMPLATE instead, which states the
    bug exactly: prose that supplies a determiner in front of a placeholder whose value supplies
    one too. A preposition in between resets the noun phrase and is fine.

    The fix is always to move the placeholder where a whole noun phrase belongs:
    "a crisp design for ${system}".
    """
    import re

    from bearpit.core.package import load_package
    from bearpit.core.params import resolve_values, scan

    project = load_package(str(EXAMPLES / name))
    found = scan(project)
    if not found:
        pytest.skip(f"{name} has no parameters")
    values = resolve_values(found, {})

    texts = {"spec.guidelines": project.spec.guidelines or "",
             "spec.restrictions": project.spec.restrictions or ""}
    for i, g in enumerate(project.spec.goals or []):
        texts[f"spec.goals[{i}]"] = g
    for a in project.agents:
        texts[f"{a.id}.persona"] = a.persona or ""
        texts[f"{a.id}.rubric"] = a.rubric or ""

    # a determiner, then at most two words that are NOT prepositions/conjunctions, then the
    # placeholder — i.e. the placeholder is completing a noun phrase already begun
    RESET = {"for", "of", "on", "in", "to", "with", "about", "and", "or", "as", "into", "from"}
    lead = re.compile(r"\b(a|an|the|one)\s+((?:\w+\s+){0,2})\$\{(\w+)", re.I)

    for where, text in texts.items():
        for m in lead.finditer(text or ""):
            between = [w.lower() for w in m.group(2).split()]
            if any(w in RESET for w in between):
                continue
            value = str(values.get(m.group(3), ""))
            if re.match(r"^\s*(a|an|the)\b", value, re.I):
                raise AssertionError(
                    f"{name}: {where} says {m.group(0)!r} and ${{{m.group(3)}}} defaults to "
                    f"{value!r} — that renders a double determiner. Put the placeholder where a "
                    f"noun phrase belongs instead."
                )
