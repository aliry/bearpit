"""The Ledger — mints per-agent virtual keys and meters spend (M4, §9).

Per agent: resolve its `api_key_ref` handle to a real credential, register that model in the
proxy (real key stays in the proxy), and mint a **virtual key** scoped to that model + the
agent's budget cap. Forge injects the virtual key into the agent's config (C10) — the agent
never sees a real key. Spend is polled and written to the Chronicle as *delta* events so the
final report's sum is correct; exhaustion is detected from cumulative spend vs cap
(never from agent chat — spike S3 finding F1).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from bearpit.chronicle import Chronicle, EventKind
from bearpit.core.plugins import hooks_for
from bearpit.core.schema import AgentSpec
from bearpit.ledger.keystore import KeyStore
from bearpit.ledger.litellm import LiteLLMClient


@dataclass(frozen=True)
class AgentCredential:
    """What Forge injects into an agent's model config (C10)."""

    virtual_key: str  # -> config.yaml model.api_key
    model_name: str  # -> config.yaml model.model (the proxy's public model name)
    proxy_url: str  # -> config.yaml model.base_url


def _effective_budget_usd(agent: AgentSpec) -> float | None:
    """The USD budget to enforce on the agent's key. LiteLLM caps by USD, so a `max_tokens` cap is
    converted to USD via the (schema-guaranteed) per-token cost — using the higher of input/output
    cost so the cap is reached at or before the token limit — and combined with `max_usd` by taking
    the tighter of the two. None = uncapped."""
    caps: list[float] = []
    if agent.budget.max_usd is not None:
        caps.append(agent.budget.max_usd)
    if agent.budget.max_tokens is not None:
        m = agent.require_model()
        cost = max(m.input_cost_per_token or 0.0, m.output_cost_per_token or 0.0)
        caps.append(agent.budget.max_tokens * cost)
    return min(caps) if caps else None


class Ledger:
    def __init__(self, keystore: KeyStore, client: LiteLLMClient, proxy_url: str) -> None:
        self._ks = keystore
        self._c = client
        self._proxy = proxy_url
        self._keys: dict[tuple[str, str], str] = {}  # (realm, agent) -> virtual key
        self._last: dict[tuple[str, str], float] = {}  # (realm, agent) -> last cumulative spend
        self._last_tok: dict[tuple[str, str], tuple[int, int]] = {}  # -> last (prompt, completion)

    @property
    def proxy_url(self) -> str:
        return self._proxy

    async def provision_agent(
        self, realm_id: str, agent: AgentSpec, *, api_key_override: str | None = None
    ) -> AgentCredential:
        m = agent.require_model()  # resolved by the provider resolver (or an explicit override)
        cred = self._ks.get(m.api_key_ref)  # resolve handle -> real key+endpoint
        model_name = f"{realm_id}--{agent.id}"
        # How this provider wants the model and its reasoning effort expressed. The default sends
        # the model name plus `reasoning_effort` as a best-effort request param the proxy drops if
        # the deployment rejects it; a provider whose runtime decodes effort from the model string
        # returns it encoded there instead, with no separate param (`core.plugins`).
        real_model, effort = hooks_for(m.provider).encode_model(m)
        # api_key_override: a per-agent request credential, supplied by the caller (Forge) when the
        # provider asks for one — it lets the far side act AS this agent. Same trust boundary as
        # every provider key: it lives only in the Ledger/LiteLLM proxy (BYOK rule).
        await self._c.register_model(
            model_name,
            real_model=real_model,
            api_key=api_key_override or cred.api_key,
            api_base=cred.api_base,
            input_cost_per_token=m.input_cost_per_token,
            output_cost_per_token=m.output_cost_per_token,
            reasoning_effort=effort,
        )
        vkey = await self._c.mint_key(
            alias=model_name, models=[model_name],
            max_budget=_effective_budget_usd(agent),
        )
        self._keys[(realm_id, agent.id)] = vkey
        return AgentCredential(virtual_key=vkey, model_name=model_name, proxy_url=self._proxy)

    def minted_keys(self, realm_id: str) -> list[str]:
        """Every virtual key this Ledger minted for `realm_id`.

        The platform minted them, so it can mask them before recording anything an agent produced
        (see `core.redact`). An agent's container holds its own key in plaintext, and `run_code`
        output goes straight into the append-only Chronicle."""
        return [key for (realm, _), key in self._keys.items() if realm == realm_id]

    async def poll_spend(
        self, realm_id: str, chronicle: Chronicle
    ) -> dict[str, tuple[float, float | None]]:
        """Poll each realm agent's cumulative spend; write the delta to the Chronicle.
        Returns {agent: (cumulative_spend, cap)} for exhaustion checks."""
        out: dict[str, tuple[float, float | None]] = {}
        for (realm, aid), vkey in list(self._keys.items()):
            if realm != realm_id:
                continue
            spend, cap = await self._c.key_spend(vkey)
            out[aid] = (spend, cap)
            try:  # token stats are for display only — never let them break spend/exhaustion polling
                tin, tout = await self._c.key_tokens(vkey)
            except Exception:  # noqa: BLE001 - any proxy/logging hiccup falls back to last-known
                tin, tout = self._last_tok.get((realm, aid), (0, 0))
            l_in, l_out = self._last_tok.get((realm, aid), (0, 0))
            d_in, d_out = max(0, tin - l_in), max(0, tout - l_out)
            delta = spend - self._last.get((realm, aid), 0.0)
            # record a SPEND event when USD OR token usage grew — tokens can flush at a slightly
            # different cadence than the aggregated key spend, so don't gate them on the USD delta.
            if delta > 1e-9 or d_in > 0 or d_out > 0:
                payload = {
                    "agent": aid, "usd": round(delta, 6), "cumulative": round(spend, 6), "cap": cap,
                    "tokens_in": d_in, "tokens_out": d_out,
                }
                await chronicle.append_event(realm_id, EventKind.SPEND, payload)
                self._last[(realm, aid)] = spend
                self._last_tok[(realm, aid)] = (tin, tout)
        return out

    @staticmethod
    def exhausted(spend_map: dict[str, tuple[float, float | None]]) -> list[str]:
        """Agents whose cumulative spend has reached their cap (Warden acts on these)."""
        return [a for a, (spend, cap) in spend_map.items() if cap is not None and spend >= cap]

    async def teardown(self, realm_id: str) -> None:
        for (realm, aid), vkey in list(self._keys.items()):
            if realm == realm_id:
                await self._c.delete_key(vkey)
                # also drop the per-agent MODEL registration — STORE_MODEL_IN_DB persists it, so
                # without this every agent-run leaks a permanent model row in the litellm DB.
                with contextlib.suppress(Exception):  # best-effort, like the key delete
                    await self._c.delete_model(f"{realm}--{aid}")
                self._keys.pop((realm, aid), None)
                self._last.pop((realm, aid), None)
