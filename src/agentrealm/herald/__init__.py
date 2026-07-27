"""Herald: Matrix AppService — rooms, ACLs (physics), message logging, injection (M3, §7)."""

from agentrealm.herald.herald import BusProvision, Herald
from agentrealm.herald.matrix import HttpMatrixClient, MatrixClient, MatrixError
from agentrealm.herald.ratelimit import RateLimiter, TokenBucket
from agentrealm.herald.types import MatrixCreds

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
