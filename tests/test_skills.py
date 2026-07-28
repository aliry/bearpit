"""Built-in role/capability skills (#37) — seeded by role, with declared additions."""

from bearpit.core.schema import AgentRole, AgentSpec, ModelRef, SkillRef
from bearpit.forge.skills import BUILTIN_SKILLS, skill_files


def _agent(aid, role=AgentRole.PARTICIPANT, skills=()):
    return AgentSpec(
        id=aid, role=role,
        model=ModelRef(provider="azure", model="m", api_key_ref="azure-main"),
        skills=list(skills),
    )


def test_role_defaults():
    participant = skill_files(_agent("vela"))
    assert "skills/agent-basics/SKILL.md" in participant
    assert "skills/referee-basics/SKILL.md" not in participant

    referee = skill_files(_agent("themis", role=AgentRole.REFEREE))
    assert "skills/referee-basics/SKILL.md" in referee
    assert "skills/agent-basics/SKILL.md" not in referee


def test_declared_builtin_added_alongside_default():
    # declaring a builtin skill adds it; the role default is still present
    agent = _agent("vela", skills=[SkillRef(source="builtin", ref="referee-basics")])
    files = skill_files(agent)
    assert "skills/agent-basics/SKILL.md" in files  # role default
    assert "skills/referee-basics/SKILL.md" in files  # declared


def test_local_skills_are_not_resolved_here():
    # local/gh skills are copied from the package by Forge, not from the builtin library
    agent = _agent("vela", skills=[SkillRef(source="local", ref="my-skill")])
    assert list(skill_files(agent)) == ["skills/agent-basics/SKILL.md"]


def test_flavor_skills_are_opt_in_per_scenario():
    # the library carries referee + participant flavors; none are seeded unless declared
    assert {"referee-scorekeeper", "referee-progress", "competitor", "collaborator"} <= set(
        BUILTIN_SKILLS
    )
    ref = _agent("judge", role=AgentRole.REFEREE,
                 skills=[SkillRef(source="builtin", ref="referee-scorekeeper")])
    files = skill_files(ref)
    assert "skills/referee-basics/SKILL.md" in files  # neutral core still seeded
    assert "skills/referee-scorekeeper/SKILL.md" in files  # chosen flavor
    assert "skills/referee-progress/SKILL.md" not in files  # other flavors NOT imposed
    coop = _agent("vela", skills=[SkillRef(source="builtin", ref="collaborator")])
    assert "skills/collaborator/SKILL.md" in skill_files(coop)
    assert "skills/competitor/SKILL.md" not in skill_files(coop)


def test_skill_content_is_valid_frontmatter():
    for name, content in BUILTIN_SKILLS.items():
        assert content.startswith("---\n")
        assert f"name: {name}" in content
        # agentskills.io: description must be <= 60 chars (S1 finding)
        desc = next(line for line in content.splitlines() if line.startswith("description:"))
        assert len(desc[len("description: "):]) <= 60


def test_gamemaster_ends_via_rule_tool():
    # The referee ends the game by CALLING the `rule` verdict tool, and checks crew-win first
    # (saboteur ejected => crew wins immediately) — the among-us-0b8cc0 fixes.
    skill = BUILTIN_SKILLS["referee-social-deduction"]
    assert "`rule`" in skill and "rule(" in skill
    # the WIN CONDITIONS themselves are the scenario's business, not the platform's — the skill must
    # send the referee to its own rubric for them, never hardcode one game's precedence rules.
    assert "rubric" in skill.lower()


def test_social_deduction_bars_voting_the_host_or_the_already_out():
    # Generic, and it survived the de-Among-Us-ification for a reason: a referee is not a player and
    # cannot be voted out, and neither can someone already eliminated. Both were real live bugs
    # (votes cast for Mother, and for a player ejected the round before).
    skill = BUILTIN_SKILLS["social-deduction"].lower()
    assert "host is not a player" in skill or "not a player" in skill
    assert "still in the game" in skill
    assert "eliminated" in skill


def test_skills_never_prohibit_tool_calls():
    # Tool calls are FREE (they don't consume a turn — the TurnManager ignores them), so no skill
    # may scare agents away from tools: the referee MUST call `rule`, and a player reading a skill
    # is harmless. The old "Do NOT call any tools" wording trained Mother to narrate the win
    # instead of calling the verdict tool (among-us-tele2).
    for name in ("social-deduction", "referee-social-deduction"):
        skill = BUILTIN_SKILLS[name].lower()
        assert "do not call any tool" not in skill and "never call them" not in skill, name
        assert "never poll" not in skill, name
    sd = BUILTIN_SKILLS["social-deduction"]
    # assert the INVARIANT, not one phrasing of it: tool calls are free, and only a chat message
    # completes a turn. (The old test pinned exact wording, so rewording a skill "failed" it.)
    assert "tool calls are free" in sd.lower()
    assert "passes the floor" in sd.lower() or "posting" in sd.lower()


# The naming convention (skills.py): CORE + CAPABILITY skills are generic and may never speak one
# scenario's language; SCENARIO-FAMILY skills are named after their family and therefore may.
_FAMILY_SKILLS = {"social-deduction", "referee-social-deduction"}


def test_only_a_family_named_skill_may_speak_a_scenarios_language():
    """A builtin skill is PLATFORM text — it ships to every scenario that names it, and it is
    injected straight into SOUL.md. So a skill that talks about saboteurs and ejections must SAY SO
    IN ITS NAME. `referee-gamemaster` did not: it read like the generic referee skill for any realm
    with rounds, and it was quietly teaching Among Us's rules (and its stale, chat-parsed voting) to
    auction clerks and debate chairs. Renamed to `referee-social-deduction`, it may speak freely."""
    import re

    from bearpit.forge.skills import BUILTIN_SKILLS

    banned = re.compile(
        r"(?i)\b(saboteur|crewmate|impostor|cygnus|among.?us|mafia|cass|vega|juno|rhea)\b"
    )
    for name, text in BUILTIN_SKILLS.items():
        hits = sorted({m.group(0).lower() for m in banned.finditer(text)})
        if name in _FAMILY_SKILLS:
            continue  # named for its family — it is allowed its family's vocabulary
        assert not hits, f"generic skill {name!r} speaks one scenario's language: {hits}"


def test_every_family_skill_is_named_after_its_family():
    # the name IS the contract that lets the skill speak freely. A family skill whose name does not
    # announce the family would be indistinguishable from generic guidance.
    from bearpit.forge.skills import BUILTIN_SKILLS

    for name in _FAMILY_SKILLS:
        assert name in BUILTIN_SKILLS
        assert "social-deduction" in name
        # ...and it says so in the first lines the model reads
        assert "social-deduction" in BUILTIN_SKILLS[name][:400]


def test_the_family_referee_skill_teaches_the_CURRENT_mechanics():
    from bearpit.forge.skills import BUILTIN_SKILLS

    gm = BUILTIN_SKILLS["referee-social-deduction"]
    # state changes are tool calls, never prose
    assert "NOTHING IS REAL UNTIL YOU CALL THE TOOL" in gm
    assert "eliminate" in gm and "rule(" in gm
    # sealed submissions are real and must be read via reveal_status -> reveal
    assert "reveal_status" in gm and "reveal(" in gm
    assert "don't apply" not in gm  # the old skill declared the sealed tools useless
    # one post per boundary — posting releases the floor
    assert "ONE post per boundary" in gm


def test_referee_basics_forbids_inferring_sealed_submissions_from_chat():
    """The easiest mistake on this platform: the transcript always LOOKS like it contains the
    answer, so a referee can produce a confident, plausible, invented tally. among-us-sim4 did
    exactly that on a different mechanic — Mother posted a full round resolution having called zero
    tools. Every referee is now told: what people SAY is not what they SUBMITTED."""
    skill = BUILTIN_SKILLS["referee-basics"]
    assert "NOT WHAT THEY SUBMITTED" in skill
    assert "reveal_status" in skill and "one-way door" in skill
    assert "tally" in skill and "Only `rule()` does" in skill  # tally scores a round, never ends it
