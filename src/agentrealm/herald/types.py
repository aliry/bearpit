"""Shared bus-identity type (Herald produces it; the Hermes adapter consumes it)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MatrixCreds:
    """The Matrix identity + room context Herald mints for one agent."""

    homeserver: str  # in-cluster URL, e.g. http://conduit:6167
    user_id: str  # @<realm>-<agent>:realm.local  (localpart never starts with '_' — C1)
    access_token: str
    allowed_users: Sequence[str]  # who this agent will respond to (system, operator, peers)
    commons_room: str  # room id; also the home channel (suppresses the setup notice, #28)
    require_mention: bool = True  # mention-gated (anti-loop, C3) vs free-response
    free_response_rooms: Sequence[str] = field(default_factory=tuple)
