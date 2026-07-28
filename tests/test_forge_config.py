"""The Hermes config renderer encodes every spike caveat — assert each one."""

import yaml

from bearpit.core.schema import AgentSpec, ModelRef
from bearpit.forge.adapters.hermes.config import MatrixCreds, render_hermes_home
from bearpit.ledger import AgentCredential


def _render(require_mention: bool = True, persona: str | None = "# Vela\nWin.") -> dict[str, str]:
    agent = AgentSpec(
        id="vela",
        model=ModelRef(provider="azure", model="gpt-5.4-mini", api_key_ref="azure-main"),
        persona=persona,
    )
    cred = AgentCredential(virtual_key="vk-1", model_name="r--vela", proxy_url="http://litellm:4000")
    matrix = MatrixCreds(
        homeserver="http://conduit:6167",
        user_id="@vela:realm.local",
        access_token="tok-abc",
        allowed_users=["@operator:realm.local", "@orin:realm.local"],
        commons_room="!commons:realm.local",
        require_mention=require_mention,
    )
    roster = ["@vela:realm.local", "@orin:realm.local"]
    return render_hermes_home(
        agent, cred, matrix, roster=roster, guidelines="Be fair.", restrictions="No sabotage."
    )


def test_c10_main_key_in_config_not_env():
    files = _render()
    cfg = yaml.safe_load(files["config.yaml"])
    assert cfg["model"]["api_key"] == "vk-1"  # C10: key in config
    assert "MATRIX_ACCESS_TOKEN" in files[".env"]
    # the virtual key must NOT leak into .env (it lives only in config.yaml, C10)
    assert "vk-1" not in files[".env"]


def test_model_points_at_proxy():
    cfg = yaml.safe_load(_render()["config.yaml"])
    assert cfg["model"]["provider"] == "custom"
    assert cfg["model"]["base_url"] == "http://litellm:4000"
    # POC-verified: Hermes reads the model name from `model.default`, not `model.model`
    assert cfg["model"]["default"] == "r--vela"
    assert "model" not in cfg["model"]


def test_onboarding_suppressed_and_tools_allowed():
    files = _render()
    cfg = yaml.safe_load(files["config.yaml"])
    assert cfg["agent"]["verify_on_stop"] is False
    assert cfg["onboarding"]["seen"]["profile_build_offered"] is True
    assert cfg["_config_version"] == 32
    # agents may open side-channels (#33)
    assert "MATRIX_TOOLS_ALLOW_ROOM_CREATE=true" in files[".env"]
    assert "MATRIX_TOOLS_ALLOW_INVITES=true" in files[".env"]


def test_self_improvement_and_memory_disabled():
    # a realm agent must obey its BIRTH config all run — no mid-realm memory/profile/skill drift
    cfg = yaml.safe_load(_render()["config.yaml"])
    assert cfg["memory"]["memory_enabled"] is False
    assert cfg["memory"]["user_profile_enabled"] is False
    assert cfg["skills"]["creation_nudge_interval"] == 0
    assert cfg["session_reset"]["mode"] == "none"


def test_c7_fail_fast_and_aux_pinned():
    cfg = yaml.safe_load(_render()["config.yaml"])
    assert cfg["agent"]["api_max_retries"] == 1  # C7
    # C2/C12: every aux task incl. title_generation pinned to main
    assert set(cfg["auxiliary"]) >= {"vision", "web_extract", "session_search", "title_generation"}
    assert all(v["provider"] == "main" for v in cfg["auxiliary"].values())


def test_env_safety_flags():
    env = _render()[".env"]
    assert "MATRIX_E2EE_MODE=off" in env  # C5
    assert "MATRIX_HOME_ROOM=!commons:realm.local" in env  # #28
    assert "HERMES_YOLO_MODE=1" in env and "HERMES_EXEC_ASK=false" in env  # C14
    assert "MATRIX_ALLOWED_USERS=@operator:realm.local,@orin:realm.local" in env


def test_mention_gating_toggle():
    gated = _render(require_mention=True)[".env"]
    assert "MATRIX_REQUIRE_MENTION=true" in gated and "THREAD_REQUIRE_MENTION=true" in gated
    free = _render(require_mention=False)[".env"]
    assert "MATRIX_FREE_RESPONSE_ROOMS=!commons:realm.local" in free
    assert "MATRIX_REQUIRE_MENTION=true" not in free


def test_system_prompt_and_soul():
    files = _render()
    assert files["SOUL.md"].startswith("# Vela")  # persona used verbatim
    sp = yaml.safe_load(files["config.yaml"])["agent"]["system_prompt"]
    assert "never use interactive ask" in sp  # autonomy clause (POC finding)
    assert "Be fair." in sp and "No sabotage." in sp  # guidelines/restrictions
    assert "@mention" in sp  # mention etiquette


def test_default_soul_when_no_persona():
    files = _render(persona=None)
    assert "autonomous agent in Bearpit" in files["SOUL.md"]


def test_realmtools_mcp_wired_when_provided():
    from bearpit.core.schema import AgentSpec, ModelRef
    from bearpit.forge.adapters.hermes.config import RealmtoolsCreds

    agent = AgentSpec(id="vela", model=ModelRef(provider="azure", model="m", api_key_ref="k"))
    cred = AgentCredential(virtual_key="vk", model_name="r--vela", proxy_url="http://p")
    matrix = MatrixCreds(
        homeserver="h", user_id="@vela:realm.local", access_token="t",
        allowed_users=[], commons_room="!c",
    )
    rt = RealmtoolsCreds(url="http://pit-realmtools:9100/mcp", token="rt-tok-123")
    files = render_hermes_home(agent, cred, matrix, realmtools=rt)
    cfg = yaml.safe_load(files["config.yaml"])
    assert cfg["mcp_servers"]["realmtools"]["url"] == "http://pit-realmtools:9100/mcp"
    # token via env interpolation (in .env, not config.yaml — C10 style)
    auth = cfg["mcp_servers"]["realmtools"]["headers"]["Authorization"]
    assert auth == "Bearer ${REALMTOOLS_TOKEN}"
    assert "REALMTOOLS_TOKEN=rt-tok-123" in files[".env"]
    assert "rt-tok-123" not in files["config.yaml"]

    # not wired when absent
    plain = render_hermes_home(agent, cred, matrix)
    assert "mcp_servers" not in yaml.safe_load(plain["config.yaml"])
    assert "REALMTOOLS_TOKEN" not in plain[".env"]


def test_send_private_guidance_only_when_agent_has_dm_channels():
    agent, cred, matrix = _bare()
    # no DM channels -> no private-messaging guidance (don't tempt a tool the agent lacks)
    sp_none = yaml.safe_load(render_hermes_home(agent, cred, matrix)["config.yaml"])
    assert "send_private" not in sp_none["agent"]["system_prompt"]
    # with a DM channel to 'scout' -> concrete send_private guidance naming the peer
    files = render_hermes_home(agent, cred, matrix, dm_rooms={"!dm:realm.local": "scout"})
    sp = yaml.safe_load(files["config.yaml"])["agent"]["system_prompt"]
    assert "send_private" in sp
    assert "scout" in sp
    assert "never appears in the Commons" in sp


def _bare(**model_kw):
    m = ModelRef(provider="azure", model="m", api_key_ref="azure-main", **model_kw)
    agent = AgentSpec(id="vela", model=m)
    cred = AgentCredential(virtual_key="vk", model_name="m", proxy_url="p")
    matrix = MatrixCreds(homeserver="h", user_id="@v:r", access_token="t", allowed_users=[],
                         commons_room="!c")
    return agent, cred, matrix


def test_context_length_defaults_and_overrides():
    cfg = yaml.safe_load(_render()["config.yaml"])
    assert cfg["model"]["context_length"] == 128000  # #50: fallback when unset
    agent, cred, matrix = _bare(context_length=32000)
    cfg2 = yaml.safe_load(render_hermes_home(agent, cred, matrix)["config.yaml"])
    assert cfg2["model"]["context_length"] == 32000  # the model's declared window is used


def test_side_channels_gated_on_project_policy():
    assert "MATRIX_TOOLS_ALLOW_ROOM_CREATE=true" in _render()[".env"]  # #51: default allowed
    agent, cred, matrix = _bare()
    env = render_hermes_home(agent, cred, matrix, allow_side_channels=False)[".env"]
    assert "MATRIX_TOOLS_ALLOW_ROOM_CREATE=false" in env
    assert "MATRIX_TOOLS_ALLOW_INVITES=false" in env


def test_referee_rubric_is_seeded_into_the_soul():
    # The rubric is the referee's private ground truth (secret roles, win rules). SOUL.md is the
    # only per-agent text Hermes injects into the model's system prompt, so the rubric MUST ride
    # there — a rubric that exists only in the manifest never reaches the model (among-us-tele3:
    # Mother hunted the filesystem for a "crew manifest", not knowing the roles her rubric named).
    from bearpit.core.schema import AgentRole

    referee = AgentSpec(
        id="mother", role=AgentRole.REFEREE,
        model=ModelRef(provider="azure", model="m", api_key_ref="azure-main"),
        persona="# Mother\nImpartial host.",
        rubric="SECRET ROLES: the SABOTEUR is Cass. Crew win when Cass is ejected.",
    )
    cred = AgentCredential(virtual_key="vk", model_name="r--mother", proxy_url="http://l:4000")
    matrix = MatrixCreds(
        homeserver="http://conduit:6167", user_id="@mother:realm.local", access_token="t",
        allowed_users=[], commons_room="!c", require_mention=True,
    )
    files = render_hermes_home(referee, cred, matrix, roster=["@mother:realm.local"])
    soul = files["SOUL.md"]
    assert soul.startswith("# Mother")  # persona intact, rubric appended
    assert "SABOTEUR is Cass" in soul  # the rubric actually reaches the model
    assert "never reveal or quote" in soul  # framed as private
    # a rubric-less participant is unchanged
    assert "SABOTEUR" not in _render()["SOUL.md"]


def test_realmtools_mcp_gets_generous_timeouts():
    # Hermes marks a server's tools "unavailable" when a call/session times out — under load a
    # slow call must not read as a dead session (tele5: referee lost eliminate/rule all run).
    import yaml as _yaml

    from bearpit.forge.adapters.hermes.config import RealmtoolsCreds

    agent = AgentSpec(
        id="vela", model=ModelRef(provider="azure", model="m", api_key_ref="azure-main"),
    )
    cred = AgentCredential(virtual_key="vk", model_name="r--vela", proxy_url="http://l:4000")
    matrix = MatrixCreds(
        homeserver="h", user_id="@vela:realm.local", access_token="t",
        allowed_users=[], commons_room="!c", require_mention=True,
    )
    files = render_hermes_home(
        agent, cred, matrix, roster=["@vela:realm.local"],
        realmtools=RealmtoolsCreds(url="http://rt:9100/mcp", token="tok"),
    )
    rt = _yaml.safe_load(files["config.yaml"])["mcp_servers"]["realmtools"]
    assert rt["timeout"] >= 120 and rt["connect_timeout"] >= 60


def test_agents_are_told_they_have_a_container_and_a_notebook():
    from bearpit.forge.adapters.hermes.config import RealmtoolsCreds

    """An agent begins EVERY turn with no memory of the last one, and it is a language model: left
    to itself it re-derives the world from the chat log and does its arithmetic in its head. It has
    a container (`run_code`) and a private notebook (`remember`/`recall`) — saying so is the whole
    point. Telemetry showed agents never once used a capability nobody told them about."""
    agent = AgentSpec(id="vela", model=ModelRef(provider="azure", model="m", api_key_ref="k"))
    cred = AgentCredential(virtual_key="vk", model_name="r--vela", proxy_url="http://p")
    matrix = MatrixCreds(
        homeserver="h", user_id="@vela:realm.local", access_token="t",
        allowed_users=[], commons_room="!c",
    )
    rt = RealmtoolsCreds(url="http://pit-realmtools:9100/mcp", token="tok")
    prompt = yaml.safe_load(
        render_hermes_home(agent, cred, matrix, realmtools=rt)["config.yaml"]
    )["agent"]["system_prompt"]
    assert "run_code" in prompt
    assert "remember" in prompt and "recall" in prompt
    assert "no memory of the last" in prompt

    # ...and an agent WITHOUT the realm's tool server is never promised tools it does not have
    bare = yaml.safe_load(
        render_hermes_home(agent, cred, matrix)["config.yaml"])["agent"]["system_prompt"]
    assert "run_code" not in bare


def test_a_shared_folder_realm_tells_the_agent_the_folder_exists_and_how_to_use_it():
    """The shared volume is mounted at /realm/shared, but a realm agent has NO file tool — its
    allowlist is the realm's MCP tools only. So `run_code` is the ONLY way to touch it, and nothing
    in the prompt ever said the folder existed. Every file-based scenario (co-author a brief, ship a
    report) was unwinnable: a deliverable, a mounted volume, and no idea either was there."""
    from bearpit.forge.adapters.hermes.config import RealmtoolsCreds

    agent = AgentSpec(id="vela", model=ModelRef(provider="azure", model="m", api_key_ref="k"))
    cred = AgentCredential(virtual_key="vk", model_name="r--vela", proxy_url="http://p")
    matrix = MatrixCreds(
        homeserver="h", user_id="@vela:realm.local", access_token="t",
        allowed_users=[], commons_room="!c",
    )
    rt = RealmtoolsCreds(url="http://pit-realmtools:9100/mcp", token="tok")

    with_folder = yaml.safe_load(render_hermes_home(
        agent, cred, matrix, realmtools=rt, shared_folder=True)["config.yaml"]
    )["agent"]["system_prompt"]
    assert "/realm/shared" in with_folder
    assert "run_code" in with_folder  # the ONLY way it can get at the files

    # a realm with no shared folder is never told about one
    without = yaml.safe_load(render_hermes_home(
        agent, cred, matrix, realmtools=rt, shared_folder=False)["config.yaml"]
    )["agent"]["system_prompt"]
    assert "/realm/shared" not in without
