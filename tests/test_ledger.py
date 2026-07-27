"""Ledger: keystore crypto + provisioning/spend/exhaustion (with a fake LiteLLM client)."""

from pathlib import Path

import pytest

from agentrealm.chronicle import Chronicle, EventKind
from agentrealm.core.schema import AgentSpec, Budget, ModelRef
from agentrealm.ledger import KeyStore, KeyStoreError, Ledger


# --- keystore ---------------------------------------------------------------
def test_keystore_roundtrip_and_persistence(tmp_path: Path):
    key = KeyStore.generate_key()
    store = KeyStore(key, tmp_path / "ks.json")
    store.put("azure-main", "REALKEY", api_base="https://x.openai.azure.com/v1", provider="azure")
    cred = store.get("azure-main")
    assert cred.api_key == "REALKEY" and cred.provider == "azure"
    # reopened with the same key sees the persisted, still-encrypted value
    assert KeyStore(key, tmp_path / "ks.json").get("azure-main").api_key == "REALKEY"
    # and the on-disk file is ciphertext, not the plaintext key
    assert "REALKEY" not in (tmp_path / "ks.json").read_text()
    # `arealm keys list` uses handles(); `keys add` on an existing handle replaces it
    assert store.handles() == ["azure-main"]
    store.put("azure-main", "NEWKEY", api_base="https://new.services.ai.azure.com/openai/v1")
    assert store.get("azure-main").api_key == "NEWKEY"  # replaced in place
    assert store.handles() == ["azure-main"]  # still one handle


def test_keystore_wrong_key_and_missing_handle(tmp_path: Path):
    KeyStore(KeyStore.generate_key(), tmp_path / "ks.json").put("h", "SECRET")
    with pytest.raises(KeyStoreError):
        KeyStore(KeyStore.generate_key(), tmp_path / "ks.json").get("h")  # wrong master key
    with pytest.raises(KeyStoreError):
        KeyStore(KeyStore.generate_key()).get("nope")  # missing handle


# --- ledger with a fake proxy -----------------------------------------------
class FakeLiteLLM:
    def __init__(self) -> None:
        self.models: list[str] = []
        self.caps: dict[str, float | None] = {}
        self.spend: dict[str, float] = {}
        self.deleted: list[str] = []
        self._n = 0

    async def register_model(self, model_name, real_model, api_key, api_base=None,
                             input_cost_per_token=None, output_cost_per_token=None,
                             reasoning_effort=None):
        assert api_key == "REALKEY"  # the real key reaches the proxy, not the agent
        self.models.append(model_name)
        self.registered = getattr(self, "registered", [])
        self.registered.append({"real_model": real_model, "reasoning_effort": reasoning_effort})

    async def mint_key(self, alias, models, max_budget):
        self._n += 1
        vk = f"vk-{self._n}"
        self.caps[vk] = max_budget
        self.spend[vk] = 0.0
        return vk

    async def key_spend(self, virtual_key):
        return self.spend[virtual_key], self.caps[virtual_key]

    async def key_tokens(self, virtual_key):
        return getattr(self, "tok", {}).get(virtual_key, (0, 0))

    async def delete_key(self, virtual_key):
        self.deleted.append(virtual_key)

    async def delete_model(self, model_name):
        self.deleted_models = getattr(self, 'deleted_models', [])
        self.deleted_models.append(model_name)

    def bump(self, virtual_key, amount):
        self.spend[virtual_key] += amount

    def bump_tokens(self, virtual_key, tin, tout):
        self.tok = getattr(self, "tok", {})
        cur = self.tok.get(virtual_key, (0, 0))
        self.tok[virtual_key] = (cur[0] + tin, cur[1] + tout)


def _agent(aid: str, cap: float | None) -> AgentSpec:
    return AgentSpec(
        id=aid,
        model=ModelRef(provider="azure", model="gpt-5.4-mini", api_key_ref="azure-main",
            input_cost_per_token=1e-7, output_cost_per_token=6e-7),
        budget=Budget(max_usd=cap),
    )


@pytest.fixture
async def chron():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    yield c
    await c.close()


async def test_provision_mints_scoped_virtual_key():
    ks = KeyStore(KeyStore.generate_key())
    ks.put("azure-main", "REALKEY", api_base="https://x/openai/v1")
    fake = FakeLiteLLM()
    ledger = Ledger(ks, fake, proxy_url="http://litellm:4000")

    cred = await ledger.provision_agent("realm1", _agent("vela", cap=1.0))
    assert cred.virtual_key == "vk-1"
    assert cred.model_name == "realm1--vela"
    assert cred.proxy_url == "http://litellm:4000"
    assert fake.models == ["realm1--vela"] and fake.caps["vk-1"] == 1.0
    # azure agent: no effort encoding, plain model name
    assert fake.registered[0] == {"real_model": "gpt-5.4-mini", "reasoning_effort": None}


async def test_a_provider_hook_can_encode_effort_into_the_model_string():
    """Some runtimes decode reasoning effort from the model name and reject a separate parameter.
    A provider plugin says so through `encode_model`; the Ledger just asks and registers whatever
    it gets back. This drives the REAL lookup, so it also proves the hook is actually wired."""
    from agentrealm.core import plugins
    from agentrealm.core.plugins import ProviderHooks

    class _Plugin:
        def profiles(self):
            return {"suffixy": {"api_key_ref": "suffixy", "categories": {}}}

        def hooks(self, provider):
            return ProviderHooks(encode_model=lambda m: (f"{m.model}::{m.effort}", None))

    class _EP:
        name = "suffixy"

        def load(self):
            return _Plugin()

    original = plugins._entry_points
    plugins._entry_points = lambda group: (
        [_EP()] if group == plugins.PROVIDER_GROUP else []
    )
    plugins.reset_plugin_cache()
    try:
        ks = KeyStore(KeyStore.generate_key())
        ks.put("suffixy", "REALKEY", api_base="http://127.0.0.1:8787/v1")
        fake = FakeLiteLLM()
        ledger = Ledger(ks, fake, "http://litellm:4000")
        agent = AgentSpec(
            id="ada",
            model=ModelRef(provider="suffixy", model="some-model", api_key_ref="suffixy",
                           input_cost_per_token=3e-6, output_cost_per_token=1.5e-5, effort="high"),
        )
        await ledger.provision_agent("r", agent)
    finally:
        plugins._entry_points = original
        plugins.reset_plugin_cache()

    # encoded in the model string; NOT also sent as reasoning_effort
    assert fake.registered[0] == {"real_model": "some-model::high", "reasoning_effort": None}


async def test_by_default_effort_is_sent_as_a_request_parameter():
    ks = KeyStore(KeyStore.generate_key())
    ks.put("azure-main", "REALKEY")
    fake = FakeLiteLLM()
    ledger = Ledger(ks, fake, "http://litellm:4000")
    agent = AgentSpec(
        id="ben",
        model=ModelRef(provider="azure", model="gpt-5.4", api_key_ref="azure-main",
                       input_cost_per_token=5e-7, output_cost_per_token=4e-6, effort="high"),
    )
    await ledger.provision_agent("r", agent)
    # the default: plain model + effort as a best-effort request param (dropped if rejected)
    assert fake.registered[0] == {"real_model": "gpt-5.4", "reasoning_effort": "high"}


async def test_spend_deltas_and_exhaustion(chron: Chronicle):
    ks = KeyStore(KeyStore.generate_key())
    ks.put("azure-main", "REALKEY")
    fake = FakeLiteLLM()
    ledger = Ledger(ks, fake, "http://litellm:4000")
    vc = await ledger.provision_agent("r", _agent("vela", cap=0.05))
    oc = await ledger.provision_agent("r", _agent("orin", cap=0.05))

    fake.bump(vc.virtual_key, 0.02)
    fake.bump_tokens(vc.virtual_key, 100, 30)
    m = await ledger.poll_spend("r", chron)
    assert m["vela"] == (0.02, 0.05) and m["orin"] == (0.0, 0.05)
    assert Ledger.exhausted(m) == []

    fake.bump(vc.virtual_key, 0.04)  # cumulative 0.06 >= cap 0.05
    fake.bump_tokens(vc.virtual_key, 50, 20)
    fake.bump(oc.virtual_key, 0.01)
    m = await ledger.poll_spend("r", chron)
    assert Ledger.exhausted(m) == ["vela"]

    # chronicle received DELTA spend events that sum to the cumulative (usd + tokens)
    spend_evs = await chron.events("r", kind=EventKind.SPEND)
    vela = [e.payload for e in spend_evs if e.payload["agent"] == "vela"]
    assert abs(sum(p["usd"] for p in vela) - 0.06) < 1e-6  # 0.02 + 0.04, not double-counted
    assert sum(p["tokens_in"] for p in vela) == 150  # 100 + 50 token deltas
    assert sum(p["tokens_out"] for p in vela) == 50  # 30 + 20

    await ledger.teardown("r")
    assert set(fake.deleted) == {vc.virtual_key, oc.virtual_key}


async def test_teardown_deletes_the_model_registration_not_just_the_key():
    """STORE_MODEL_IN_DB persists a model row per agent. teardown deleted only the virtual key, so
    every agent of every realm ever run left a permanent model registration in the litellm DB,
    bloating model resolution forever."""
    ks = KeyStore(KeyStore.generate_key())
    ks.put("azure-main", "REALKEY", api_base="https://x/openai/v1")

    class _Client(FakeLiteLLM):
        deleted_models: list[str] = []

        async def delete_model(self, model_name):
            _Client.deleted_models.append(model_name)

    ledger = Ledger(ks, _Client(), proxy_url="http://litellm:4000")
    await ledger.provision_agent("r1", _agent("vela", cap=5.0))
    await ledger.teardown("r1")
    assert "r1--vela" in _Client.deleted_models
