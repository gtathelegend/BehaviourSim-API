"""In-memory concurrency limiter for user-scoped task execution."""

import logging
import threading
import uuid
from typing import Dict, Union

logger = logging.getLogger("behaviorsim_api.core.concurrency")


class ConcurrencyLimiter:
    """Thread-safe in-memory concurrency limiter tracking active operations per user.

    Enforces plan-specific concurrency caps (e.g. max 1 concurrent simulation on Free plan)
    in a single-instance environment without requiring distributed infrastructure.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_counts: Dict[uuid.UUID, int] = {}

    def _normalize_user_id(self, user_id: Union[uuid.UUID, str]) -> uuid.UUID:
        if isinstance(user_id, uuid.UUID):
            return user_id
        return uuid.UUID(str(user_id))

    def acquire(self, user_id: Union[uuid.UUID, str], limit: int) -> bool:
        """Attempt to acquire an execution slot for the given user.

        Args:
            user_id: The UUID or UUID string of the authenticated user.
            limit: The maximum allowed concurrent executions for the user's plan.

        Returns:
            True if slot was successfully acquired; False if limit was reached.
        """
        uid = self._normalize_user_id(user_id)
        with self._lock:
            current = self._active_counts.get(uid, 0)
            if current >= limit:
                logger.warning(
                    "Concurrency limit reached for user_id=%s (current=%s, limit=%s)",
                    uid,
                    current,
                    limit,
                )
                return False
            self._active_counts[uid] = current + 1
            return True

    def release(self, user_id: Union[uuid.UUID, str]) -> int:
        """Release an execution slot for the given user.

        Cleans up the dictionary entry when active count returns to 0.

        Returns:
            Remaining active count for the user.
        """
        uid = self._normalize_user_id(user_id)
        with self._lock:
            current = self._active_counts.get(uid, 0)
            if current <= 1:
                self._active_counts.pop(uid, None)
                return 0
            else:
                rem = current - 1
                self._active_counts[uid] = rem
                return rem

    def get_active_count(self, user_id: Union[uuid.UUID, str]) -> int:
        """Return the current active operation count for a user."""
        uid = self._normalize_user_id(user_id)
        with self._lock:
            return self._active_counts.get(uid, 0)

    def reset(self) -> None:
        """Reset all concurrency tracking state (primarily for test isolation)."""
        with self._lock:
            self._active_counts.clear()


# Process-wide singleton limiter instance
default_concurrency_limiter = ConcurrencyLimiter()
