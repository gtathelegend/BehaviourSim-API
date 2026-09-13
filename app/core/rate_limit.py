"""Rate limiting abstractions, in-memory sliding-window limiter, and FastAPI dependency."""

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple

from fastapi import Depends, status

from app.core.auth import AuthenticatedPrincipal, get_current_principal
from app.core.errors import BehaviorSimAPIError

logger = logging.getLogger("behaviorsim_api.core.rate_limit")


class RateLimiter(ABC):
    """Abstract interface for request rate limiting."""

    @abstractmethod
    def check_rate_limit(self, key: str, limit: int, window_seconds: int = 60) -> Tuple[bool, int]:
        """Check if request is allowed.

        Returns:
            Tuple of (is_allowed: bool, retry_after_seconds: int)
        """
        pass

    @abstractmethod
    def reset(self) -> None:
        """Reset internal rate limiting state (primarily for testing)."""
        pass


class InMemoryRateLimiter(RateLimiter):
    """Thread-safe in-memory sliding-window rate limiter per key.

    Note: Suitable for single-process development and testing. For distributed
    horizontal scaling in later production phases, a Redis-backed RateLimiter
    can implement the same RateLimiter interface.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, List[float]] = {}
        self._sweep_counter: int = 0

    def check_rate_limit(self, key: str, limit: int, window_seconds: int = 60) -> Tuple[bool, int]:
        now = time.time()
        window_start = now - window_seconds

        with self._lock:
            # Opportunistic sweep of expired keys periodically
            self._sweep_counter += 1
            if self._sweep_counter >= 100:
                self._sweep_counter = 0
                dead_keys = [
                    k for k, timestamps in self._records.items()
                    if not timestamps or timestamps[-1] <= window_start
                ]
                for k in dead_keys:
                    self._records.pop(k, None)

            timestamps = self._records.get(key, [])
            # Evict timestamps outside the current window
            valid_timestamps = [ts for ts in timestamps if ts > window_start]

            if len(valid_timestamps) >= limit:
                oldest = valid_timestamps[0]
                retry_after = max(1, int(oldest + window_seconds - now) + 1)
                self._records[key] = valid_timestamps
                return False, retry_after

            valid_timestamps.append(now)
            self._records[key] = valid_timestamps
            return True, 0

    def prune_expired(self, window_seconds: int = 60) -> int:
        """Prune keys whose timestamps are all older than window_seconds."""
        now = time.time()
        window_start = now - window_seconds
        with self._lock:
            dead_keys = [
                k for k, timestamps in self._records.items()
                if not timestamps or timestamps[-1] <= window_start
            ]
            for k in dead_keys:
                self._records.pop(k, None)
            return len(dead_keys)

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._sweep_counter = 0


# Default singleton rate limiter instance
default_rate_limiter = InMemoryRateLimiter()


def get_rate_limiter() -> RateLimiter:
    """Return the active RateLimiter instance."""
    return default_rate_limiter


async def check_rate_limit(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    """FastAPI dependency enforcing user plan request rate limits."""
    user = principal.user
    limit = user.plan.requests_per_minute if user.plan else 5

    allowed, retry_after = limiter.check_rate_limit(
        key=str(user.id),
        limit=limit,
        window_seconds=60,
    )

    if not allowed:
        logger.warning("Rate limit exceeded for user_id=%s (limit=%s/min)", user.id, limit)
        raise BehaviorSimAPIError(
            message=f"Rate limit exceeded. Maximum allowed: {limit} requests per minute.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            details={
                "code": "rate_limit_exceeded",
                "limit": limit,
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )
