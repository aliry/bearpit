"""LiteLLM proxy admin client (M4, §9). Endpoints verified in spike S3.

`LiteLLMClient` is a Protocol so the Ledger's logic can be unit-tested with a fake; the
`HttpLiteLLMClient` is the real implementation, integration-tested against the stack.

Routing note: models are registered on the OpenAI-compatible route (`openai/<model>` +
`api_base`), which covers Azure (v1 endpoint, verified in S2/S3), OpenAI, and local
vLLM/Ollama. Native Anthropic/Gemini routes are a later addition.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx


class LiteLLMClient(Protocol):
    async def register_model(
        self,
        model_name: str,
        real_model: str,
        api_key: str,
        api_base: str | None = None,
        input_cost_per_token: float | None = None,
        output_cost_per_token: float | None = None,
        reasoning_effort: str | None = None,
    ) -> None: ...
    async def mint_key(self, alias: str, models: list[str], max_budget: float | None) -> str: ...
    async def key_spend(self, virtual_key: str) -> tuple[float, float | None]: ...
    async def key_tokens(self, virtual_key: str) -> tuple[int, int]: ...
    async def delete_key(self, virtual_key: str) -> None: ...
    async def delete_model(self, model_name: str) -> None: ...


class HttpLiteLLMClient:
    """Talks to a running LiteLLM proxy's admin API using the master key."""

    def __init__(self, base_url: str, master_key: str, timeout: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {master_key}"}
        self._timeout = timeout

    async def register_model(
        self,
        model_name: str,
        real_model: str,
        api_key: str,
        api_base: str | None = None,
        input_cost_per_token: float | None = None,
        output_cost_per_token: float | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        # drop_params: strip params the upstream model rejects (e.g. gpt-5 reasoning models only
        # accept temperature=1, but Hermes sends its own temperature) — else the call 429s and the
        # deployment gets cooled down. Dropping an unsupported param is always safe.
        params: dict[str, Any] = {
            "model": f"openai/{real_model}", "api_key": api_key, "drop_params": True,
        }
        if api_base:
            params["api_base"] = api_base
        # explicit per-token costs are REQUIRED for budget tracking on a custom route (S3 F4)
        if input_cost_per_token is not None:
            params["input_cost_per_token"] = input_cost_per_token
        if output_cost_per_token is not None:
            params["output_cost_per_token"] = output_cost_per_token
        # a default reasoning_effort forwarded on every request (dropped if the upstream rejects
        # it). A provider that encodes effort into the model string instead passes None here —
        # see `core.plugins.ProviderHooks.encode_model`.
        if reasoning_effort is not None:
            params["reasoning_effort"] = reasoning_effort
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                f"{self._base}/model/new", headers=self._headers,
                json={"model_name": model_name, "litellm_params": params},
            )
            r.raise_for_status()

    async def mint_key(self, alias: str, models: list[str], max_budget: float | None) -> str:
        body: dict[str, Any] = {"key_alias": alias, "models": models}
        if max_budget is not None:
            body["max_budget"] = max_budget
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(f"{self._base}/key/generate", headers=self._headers, json=body)
            r.raise_for_status()
            key = r.json()["key"]
            return str(key)

    async def key_spend(self, virtual_key: str) -> tuple[float, float | None]:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(
                f"{self._base}/key/info", headers=self._headers, params={"key": virtual_key}
            )
            r.raise_for_status()
            info = r.json()["info"]
            return float(info.get("spend") or 0.0), info.get("max_budget")

    async def key_tokens(self, virtual_key: str) -> tuple[int, int]:
        """Cumulative (prompt, completion) tokens for a key, summed from its spend logs. `/key/info`
        only exposes USD, so this reads the per-request logs (indexed by key). Logs flush a few
        seconds after each call, so this is eventually-consistent — fine for a live/stored display.
        Returns (0, 0) if logging is unavailable."""
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(
                f"{self._base}/spend/logs", headers=self._headers, params={"api_key": virtual_key}
            )
            r.raise_for_status()
            rows = r.json()
        if not isinstance(rows, list):
            return 0, 0
        pin = sum(int(x.get("prompt_tokens") or 0) for x in rows if isinstance(x, dict))
        pout = sum(int(x.get("completion_tokens") or 0) for x in rows if isinstance(x, dict))
        return pin, pout

    async def delete_key(self, virtual_key: str) -> None:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(
                f"{self._base}/key/delete", headers=self._headers, json={"keys": [virtual_key]}
            )
            r.raise_for_status()

    async def delete_model(self, model_name: str) -> None:
        # STORE_MODEL_IN_DB=True persists model rows in the litellm DB; without this every agent of
        # every realm ever run leaves a permanent registration, bloating model resolution forever.
        # /model/delete wants the model's id, which we look up by its public model_name.
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            info = await c.get(f"{self._base}/model/info", headers=self._headers)
            info.raise_for_status()
            model_id = next(
                (m.get("model_info", {}).get("id")
                 for m in info.json().get("data", [])
                 if m.get("model_name") == model_name),
                None,
            )
            if model_id is None:
                return  # already gone, or never registered
            r = await c.post(
                f"{self._base}/model/delete", headers=self._headers, json={"id": model_id}
            )
            r.raise_for_status()
