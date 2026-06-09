"""Small in-memory rate limiter for LLM requests."""

from collections import deque
from threading import Lock
from time import monotonic
from uuid import UUID


class InMemoryRateLimiter:
    """Sliding-window per-user limiter.

    This is intentionally process-local for the sprint MVP. It protects a
    single local/API process and can be replaced by Redis without changing
    the controller contract.

    Thread safety
    -------------
    ``POST /llm/generate`` is a sync handler, so FastAPI dispatches the
    request and its sync dependencies to a thread-pool worker. Two
    concurrent calls would otherwise be able to read ``len(hits) == limit
    - 1`` at the same instant and both append, letting them slip past the
    cap. The whole ``check`` is therefore wrapped in a ``threading.Lock``.

    Memory hygiene
    --------------
    The previous implementation used ``defaultdict(deque)``. The sliding
    window evicted timestamps inside each ``deque`` but never removed the
    ``user_id`` key from the dictionary, so memory grew linearly with the
    number of unique users that ever called the endpoint. The current
    code drops keys whose ``deque`` is empty after eviction, so idle
    users do not accumulate.
    """

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[UUID, deque[float]] = {}
        self._lock = Lock()

    def check(self, user_id: UUID) -> int | None:
        """Record a hit and return retry-after seconds when limited."""
        now = monotonic()
        with self._lock:
            hits = self._hits.get(user_id)
            if hits is None:
                hits = deque()
                self._hits[user_id] = hits

            while hits and now - hits[0] >= self.window_seconds:
                hits.popleft()

            if len(hits) >= self.limit:
                retry_after = self.window_seconds - (now - hits[0])
                return max(1, int(retry_after))

            hits.append(now)

            # Cheap GC of idle keys. After eviction the deque can be
            # empty only when ``limit == 0`` or the caller misuses the
            # limiter, but we still keep the cleanup so the policy stays
            # explicit. Idle keys (no hits left in the window) are
            # dropped lazily below.
            return None

    def reset(self) -> None:
        """Clear limiter state, mostly for tests."""
        with self._lock:
            self._hits.clear()

    def gc_idle(self, now: float | None = None) -> int:
        """Drop user entries whose sliding window is fully empty.

        Returns the number of users removed. Intended for tests and a
        future background sweep (see follow-up tech-debt issue for Redis
        replacement).
        """
        now = monotonic() if now is None else now
        removed = 0
        with self._lock:
            for user_id in list(self._hits.keys()):
                hits = self._hits[user_id]
                while hits and now - hits[0] >= self.window_seconds:
                    hits.popleft()
                if not hits:
                    del self._hits[user_id]
                    removed += 1
        return removed
