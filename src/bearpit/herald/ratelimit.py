"""Per-sender bus rate limiting (#30).

The POC exposed a token-free failure mode: gateway error/status notices woke peer agents,
whose failed calls emitted more notices — a self-sustaining message cascade at *zero* model
cost, so budget caps couldn't stop it. The intended fix is physics at the bus boundary: a
per-sender token bucket applied to the firehose. Time is injected (`now`) so the logic is
deterministic and fully testable.

STATUS: built and tested, NOT yet wired into Herald. Said plainly because the previous wording
claimed Herald applied it, which made a real, observed failure mode look mitigated when it is not.
Wiring it means dropping messages on a live bus, so it wants a live test rather than a quiet
switch-on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TokenBucket:
    """Refills at `rate` tokens/sec up to `capacity`; `allow` consumes one if available."""

    rate: float
    capacity: float

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last: float | None = None

    def allow(self, now: float) -> bool:
        if self._last is None:
            self._last = now
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class RateLimiter:
    """A token bucket per sender. Default: sustained 0.5 msg/s with a burst of 5."""

    def __init__(self, rate: float = 0.5, capacity: float = 5.0) -> None:
        self._rate = rate
        self._capacity = capacity
        self._buckets: dict[str, TokenBucket] = {}

    def allow(self, sender: str, now: float) -> bool:
        bucket = self._buckets.get(sender)
        if bucket is None:
            bucket = TokenBucket(self._rate, self._capacity)
            self._buckets[sender] = bucket
        return bucket.allow(now)
