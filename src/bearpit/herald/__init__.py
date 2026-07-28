"""Herald: Matrix AppService — rooms, ACLs (physics), message logging, injection (M3, §7)."""

from bearpit.herald.herald import BusProvision, Herald
from bearpit.herald.matrix import HttpMatrixClient, MatrixClient, MatrixError
from bearpit.herald.ratelimit import RateLimiter, TokenBucket
from bearpit.herald.types import MatrixCreds

__all__ = [
    "BusProvision",
    "Herald",
    "HttpMatrixClient",
    "MatrixClient",
    "MatrixCreds",
    "MatrixError",
    "RateLimiter",
    "TokenBucket",
]
