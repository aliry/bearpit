"""Matrix client for Herald (M3). Endpoints verified in spikes S2/S3.

`MatrixClient` is a Protocol so Herald's orchestration tests with a fake; `HttpMatrixClient`
is the real async implementation against a Conduit homeserver. Caveat C13: Conduit 500s on a
malformed (empty) auth header, so we omit the header entirely when there is no token.
"""

from __future__ import annotations

import secrets
from typing import Any, Protocol

import httpx


class MatrixError(RuntimeError):
    pass


class MatrixClient(Protocol):
    async def register_or_login(self, username: str, password: str) -> str: ...
    async def create_room(self, token: str, name: str, invite: list[str]) -> str: ...
    async def invite(self, token: str, room_id: str, user_id: str) -> None: ...
    async def join(self, token: str, room_id: str) -> None: ...
    async def send(
        self, token: str, room_id: str, body: str, msgtype: str = "m.text",
        mentions: list[str] | None = None,
    ) -> str: ...
    async def messages(
        self, token: str, room_id: str, limit: int = 100
    ) -> list[dict[str, Any]]: ...
    async def room_members(self, token: str, room_id: str) -> list[str]: ...
    async def set_power_levels(
        self, token: str, room_id: str, users: dict[str, int], events_default: int
    ) -> None: ...


class HttpMatrixClient:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    async def _req(
        self, method: str, path: str, token: str | None = None,
        json: dict[str, Any] | None = None, params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}  # C13: omit if no token
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.request(
                method, self._base + path, headers=headers, json=json, params=params
            )
            r.raise_for_status()
            return r.json() if r.content else {}

    async def register_or_login(self, username: str, password: str) -> str:
        body = {"auth": {"type": "m.login.dummy"}, "username": username, "password": password}
        try:
            d = await self._req("POST", "/_matrix/client/v3/register", json=body)
        except httpx.HTTPStatusError:  # user exists -> log in with the same password
            login = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": username},
                "password": password,
            }
            d = await self._req("POST", "/_matrix/client/v3/login", json=login)
        return str(d["access_token"])

    async def create_room(self, token: str, name: str, invite: list[str]) -> str:
        d = await self._req(
            "POST", "/_matrix/client/v3/createRoom", token=token,
            json={"name": name, "preset": "private_chat", "invite": invite},
        )
        return str(d["room_id"])

    async def invite(self, token: str, room_id: str, user_id: str) -> None:
        await self._req(
            "POST", f"/_matrix/client/v3/rooms/{room_id}/invite", token=token,
            json={"user_id": user_id},
        )

    async def join(self, token: str, room_id: str) -> None:
        await self._req("POST", f"/_matrix/client/v3/join/{room_id}", token=token, json={})

    async def send(
        self, token: str, room_id: str, body: str, msgtype: str = "m.text",
        mentions: list[str] | None = None,
    ) -> str:
        content: dict[str, Any] = {"msgtype": msgtype, "body": body}
        if mentions:
            # Hermes only reacts to intentional mentions (m.mentions) + an HTML pill, not a
            # plain-text mxid (verified live). Prepend pills so addressed agents engage.
            text_pills = " ".join(mentions)
            html_pills = " ".join(f'<a href="https://matrix.to/#/{u}">{u}</a>' for u in mentions)
            content["body"] = f"{text_pills} {body}"
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = f"{html_pills} {body}"
            content["m.mentions"] = {"user_ids": list(mentions)}
        txn = secrets.token_hex(8)
        d = await self._req(
            "PUT", f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
            token=token, json=content,
        )
        return str(d["event_id"])

    async def messages(self, token: str, room_id: str, limit: int = 100) -> list[dict[str, Any]]:
        d = await self._req(
            "GET", f"/_matrix/client/v3/rooms/{room_id}/messages", token=token,
            params={"dir": "b", "limit": str(limit)},
        )
        chunk: list[dict[str, Any]] = d.get("chunk", [])
        return chunk

    async def room_members(self, token: str, room_id: str) -> list[str]:
        d = await self._req(
            "GET", f"/_matrix/client/v3/rooms/{room_id}/joined_members", token=token
        )
        return list(d.get("joined", {}))

    async def set_power_levels(
        self, token: str, room_id: str, users: dict[str, int], events_default: int
    ) -> None:
        """Replace the room's `m.room.power_levels`. `events_default` is the level required to
        post a message; users below it are muted at the homeserver (out-of-turn posts get 403).
        Do NOT list the room creator (system): in room v11+ it has implicit infinite power and
        listing it is rejected, so it can always post + change the floor without an entry. Only
        PL 100 may change state (state_default), so a floor-holder (50) cannot grant itself
        permanent power."""
        content = {
            "users": users,
            "users_default": 0,
            "events_default": events_default,
            "state_default": 100,
            "ban": 100, "kick": 100, "redact": 100, "invite": 100,
            "events": {"m.room.power_levels": 100, "m.room.name": 100, "m.room.topic": 100},
        }
        await self._req(
            "PUT", f"/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels",
            token=token, json=content,
        )
