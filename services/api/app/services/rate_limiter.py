"""Atomic, namespaced Redis rate limiting without sensitive raw keys."""

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from time import time


class RateLimitFailureMode(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class RateLimitPolicy:
    namespace: str
    limit: int
    window_seconds: int
    failure_mode: RateLimitFailureMode = RateLimitFailureMode.OPEN

    def __post_init__(self) -> None:
        if (
            not isinstance(self.namespace, str)
            or not self.namespace
            or self.namespace != self.namespace.strip()
            or isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit < 1
            or isinstance(self.window_seconds, bool)
            or not isinstance(self.window_seconds, int)
            or self.window_seconds < 1
            or not isinstance(self.failure_mode, RateLimitFailureMode)
        ):
            raise ValueError("invalid rate-limit policy")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    reset_at: int
    retry_after: int


class RedisRateLimiter:
    """Use one Lua script so increment and expiry are indivisible."""

    _SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""

    def __init__(self, client: object, clock: object = time) -> None:
        self._client, self._clock = client, clock

    @staticmethod
    def derive_key(namespace: str, identifier: object) -> str:
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("rate-limit identifier is invalid")
        digest = hashlib.sha256(f"{namespace}:{identifier}".encode()).hexdigest()
        return f"rate:{namespace}:{digest}"

    def consume(self, policy: RateLimitPolicy, identifier: object) -> RateLimitDecision:
        now = int(self._clock())
        try:
            count, ttl = self._client.eval(self._SCRIPT, 1, self.derive_key(policy.namespace, identifier), policy.window_seconds)
            ttl = max(0, int(ttl))
            return RateLimitDecision(int(count) <= policy.limit, max(0, policy.limit - int(count)), now + ttl, ttl if int(count) > policy.limit else 0)
        except Exception:
            if policy.failure_mode is RateLimitFailureMode.OPEN:
                return RateLimitDecision(True, policy.limit, now, 0)
            return RateLimitDecision(False, 0, now + policy.window_seconds, policy.window_seconds)
