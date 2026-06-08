"""Small in-memory rate limiter for LLM requests."""

from collections import defaultdict, deque
from time import monotonic
from uuid import UUID


class InMemoryRateLimiter:
    """Sliding-window per-user limiter.

    This is intentionally process-local for the sprint MVP. It protects a
    single local/API process and can be replaced by Redis without changing
    the controller contract.
    """

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[UUID, deque[float]] = defaultdict(deque)

    def check(self, user_id: UUID) -> int | None:
        """Record a hit and return retry-after seconds when limited."""
        now = monotonic()
        hits = self._hits[user_id]
        while hits and now - hits[0] >= self.window_seconds:
            hits.popleft()

        if len(hits) >= self.limit:
            retry_after = self.window_seconds - (now - hits[0])
            return max(1, int(retry_after))

        hits.append(now)
        return None

    def reset(self) -> None:
        """Clear limiter state, mostly for tests."""
        self._hits.clear()
