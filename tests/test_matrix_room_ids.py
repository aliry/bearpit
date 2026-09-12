"""The room id a realm is given must be its own — Conduit's is not, under concurrency."""
import asyncio

import pytest

from bearpit.herald.matrix import HttpMatrixClient, RoomIdCollision


class _RacyTransport:
    """Stands in for the pinned Conduit, which hands out duplicate room ids to concurrent
    createRoom calls. Measured against the real thing: 8 sequential calls -> 8 distinct rooms,
    8 concurrent calls -> 4. Distinct room NAMES do not help (7 of 8), so it is the id generation
    racing, not name deduplication.

    Here every call that overlaps another returns the same id, which is the same failure amplified
    to make the test deterministic.
    """

    def __init__(self) -> None:
        self.in_flight = 0
        self.issued: list[str] = []
        self.batch = 0

    async def __call__(self, method, path, token=None, json=None, params=None):
        self.in_flight += 1
        mine = self.in_flight
        if mine == 1:
            self.batch += 1
        await asyncio.sleep(0)  # let the other callers interleave
        room = f"!racy{self.batch}:realm.local"
        self.in_flight -= 1
        self.issued.append(room)
        return {"room_id": room}


def _client(transport):
    c = HttpMatrixClient("http://conduit")
    c._req = transport  # type: ignore[method-assign]
    return c


async def test_concurrent_room_creation_never_hands_two_realms_the_same_room():
    """camp-research-brief and camp-reverse-auction launched in the same second and were both given
    `!Oh9ACyNB6_rqz4WjTbcK03TJxDgKV-8W8Pne8y-65Bc`. A sealed auction bid from one then appears in
    the other's chronicle, under its own realm_id:

        camp-research-brief | @camp-reverse-auction-athena | "Bid already sealed at 42."

    Realm isolation is a hard guarantee, so the client serialises creation rather than hoping.
    """
    c = _client(_RacyTransport())
    rooms = await asyncio.gather(
        *[c.create_room("tok", f"Realm Commons {i}", []) for i in range(8)]
    )
    assert len(set(rooms)) == 8, f"two realms share a room: {sorted(rooms)}"


async def test_a_duplicate_room_id_is_refused_rather_than_used():
    """The lock only covers one process. If a duplicate ever does come back, provisioning must fail
    loudly — two realms silently sharing a commons is the worst outcome available, and it is
    exactly what happened: nothing errored and both realms ran to completion reporting success.
    """
    class _AlwaysSame:
        async def __call__(self, *a, **k):
            return {"room_id": "!same:realm.local"}

    c = _client(_AlwaysSame())
    first = await c.create_room("tok", "Realm Commons", [])
    assert first == "!same:realm.local"
    with pytest.raises(RoomIdCollision):
        await c.create_room("tok", "Realm Commons", [])
