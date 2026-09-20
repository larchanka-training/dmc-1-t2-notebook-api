"""Stale LLM reservation reconciliation service (Roadmap Step 8e-3).

Finds and closes orphaned reservations older than a configured threshold:
- 'reserved' -> 'released': returns both calls_reserved and cost_reserved_micros
  across all 4 counter rows (global/day, global/month, user/day, user/month).
- 'started' -> 'unknown': leaves quota counters unchanged, appends exactly one
  ledger event with status='unknown', call_kind preserved, and model_id=None.

Reference: api/docs/llm-usage-controls.md §5.3, §10
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, settings as app_settings
from app.core.logging import get_logger
from app.modules.llm.repositories import LlmUsageRepository
from app.modules.llm.schemas.llm_schemas import LlmReconciliationSummary
from app.modules.llm.services.usage_service import LlmUsageService

logger = get_logger(__name__)


class LlmReconciliationService:
    """Reconciles stale open LLM reservations according to canonical contract §5.3."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or app_settings

    def reconcile_stale_reservations(
        self,
        *,
        now: datetime | None = None,
        stale_seconds: int | None = None,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> LlmReconciliationSummary:
        """Find and reconcile reservations in 'reserved' or 'started' state older than cutoff.

        Args:
            now: Current timestamp (defaults to datetime.now(UTC)).
            stale_seconds: Seconds threshold (defaults to settings.llm_reconciliation_stale_seconds).
            limit: Batch limit (defaults to settings.llm_reconciliation_batch_size).
            dry_run: If True, reports what would be reconciled without making mutations.

        Returns:
            LlmReconciliationSummary with counts and returned cost.
        """
        if now is None:
            now_ts = datetime.now(UTC)
        elif now.tzinfo is None:
            now_ts = now.replace(tzinfo=UTC)
        else:
            now_ts = now.astimezone(UTC)

        threshold = (
            stale_seconds
            if stale_seconds is not None
            else self.settings.llm_reconciliation_stale_seconds
        )
        cutoff = now_ts - timedelta(seconds=threshold)
        batch_limit = (
            limit if limit is not None else self.settings.llm_reconciliation_batch_size
        )

        with self.session_factory() as session:
            repo = LlmUsageRepository(session)
            candidates = repo.get_stale_reservations(cutoff, limit=batch_limit)
            # Unpack fields to detach completely from the query session
            unpacked = [
                (
                    res.id,
                    res.state,
                    res.user_id,
                    res.request_id,
                    res.call_kind,
                    res.provider,
                    res.cost_reserved_micros,
                    res.created_at,
                )
                for res in candidates
            ]

        reconciled_reserved = 0
        reconciled_started = 0
        returned_cost_micros = 0

        if dry_run:
            for (
                _id,
                state,
                _user_id,
                _req_id,
                _call_kind,
                _provider,
                cost_micros,
                _created,
            ) in unpacked:
                if state == "reserved":
                    reconciled_reserved += 1
                    returned_cost_micros += cost_micros
                elif state == "started":
                    reconciled_started += 1
            return LlmReconciliationSummary(
                reconciled_reserved=reconciled_reserved,
                reconciled_started=reconciled_started,
                returned_cost_micros=returned_cost_micros,
                cutoff=cutoff,
            )

        for (
            res_id,
            state,
            user_id,
            request_id,
            call_kind,
            provider,
            cost_reserved,
            created_at,
        ) in unpacked:
            with self.session_factory() as session:
                repo = LlmUsageRepository(session)
                if state == "reserved":
                    # Step 1: Atomic state claim on reservation
                    returned_bound = repo.transition_reservation_to_released(
                        res_id, closed_at=now_ts
                    )
                    if returned_bound is None:
                        # Already transitioned or settled concurrently
                        session.rollback()
                        continue

                    # Step 2: Decrement quota counters on all 4 rows in canonical order
                    day_start, month_start = LlmUsageService.get_window_dates(
                        created_at
                    )
                    user_id_str = str(user_id)
                    scopes = [
                        ("global", "-", "day", day_start),
                        ("global", "-", "month", month_start),
                        ("user", user_id_str, "day", day_start),
                        ("user", user_id_str, "month", month_start),
                    ]
                    for scope, scope_key, window_kind, window_start in scopes:
                        repo.release_quota(
                            scope=scope,
                            scope_key=scope_key,
                            window_kind=window_kind,
                            window_start=window_start,
                            cost_reserved_micros=returned_bound,
                            call_count=1,
                        )
                    session.commit()
                    reconciled_reserved += 1
                    returned_cost_micros += returned_bound

                elif state == "started":
                    # Step 1: Atomic state claim on reservation
                    transitioned = repo.transition_reservation_to_unknown(
                        res_id, closed_at=now_ts
                    )
                    if not transitioned:
                        # Already settled or transitioned concurrently
                        session.rollback()
                        continue

                    # Step 2: Leave counters unchanged; append ledger event with status='unknown'
                    repo.record_event(
                        user_id=user_id,
                        request_id=request_id,
                        reservation_id=res_id,
                        call_kind=call_kind,
                        provider=provider,
                        model_id=None,
                        status="unknown",
                        prompt_tokens=0,
                        completion_tokens=0,
                        estimated_cost_micros=cost_reserved,
                        created_at=now_ts,
                    )
                    session.commit()
                    reconciled_started += 1

        logger.info(
            "llm.reconciliation.completed",
            reconciled_reserved=reconciled_reserved,
            reconciled_started=reconciled_started,
            returned_cost_micros=returned_cost_micros,
            cutoff=cutoff.isoformat(),
        )

        return LlmReconciliationSummary(
            reconciled_reserved=reconciled_reserved,
            reconciled_started=reconciled_started,
            returned_cost_micros=returned_cost_micros,
            cutoff=cutoff,
        )
