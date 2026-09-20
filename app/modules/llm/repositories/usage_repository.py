"""Data-access layer for LLM usage accounting, reservations, counters, and entitlements."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import inspect, select, text, update
from sqlalchemy.orm import Session

from app.modules.llm.models import (
    LlmEntitlement,
    LlmUsageCounter,
    LlmUsageEvent,
    LlmUsageReservation,
)


class LlmUsageRepository:
    """Repository for managing LLM usage controls in schema ``users``."""

    def __init__(self, db: Session) -> None:
        """Bind the repository to a SQLAlchemy session."""
        self.db = db

    # -------------------------------------------------------------------------
    # Entitlements
    # -------------------------------------------------------------------------

    def get_entitlement(self, user_id: UUID) -> LlmEntitlement | None:
        """Fetch entitlement record for a specific user."""
        return self.db.get(LlmEntitlement, user_id, populate_existing=True)

    def upsert_entitlement(
        self,
        *,
        user_id: UUID,
        tier: str,
        daily_call_limit: int | None = None,
        monthly_call_limit: int | None = None,
        valid_until: datetime | None = None,
        now: datetime | None = None,
    ) -> LlmEntitlement:
        """Upsert per-user tier and limit overrides."""
        now_ts = now or datetime.now(UTC)
        entitlement = self.db.get(LlmEntitlement, user_id)
        if entitlement is None:
            entitlement = LlmEntitlement(
                user_id=user_id,
                tier=tier,
                daily_call_limit=daily_call_limit,
                monthly_call_limit=monthly_call_limit,
                valid_until=valid_until,
                created_at=now_ts,
                updated_at=now_ts,
            )
            self.db.add(entitlement)
        else:
            entitlement.tier = tier
            entitlement.daily_call_limit = daily_call_limit
            entitlement.monthly_call_limit = monthly_call_limit
            entitlement.valid_until = valid_until
            entitlement.updated_at = now_ts
        self.db.flush()
        return entitlement

    # -------------------------------------------------------------------------
    # Reservations
    # -------------------------------------------------------------------------

    def _refresh_loaded_reservation(self, reservation_id: UUID) -> None:
        """Refresh an already-loaded reservation object in the session identity map."""
        key = inspect(LlmUsageReservation).identity_key_from_primary_key(
            (reservation_id,)
        )
        obj = self.db.identity_map.get(key)
        if obj is not None:
            self.db.refresh(obj)

    def create_reservation(
        self,
        *,
        user_id: UUID,
        request_id: UUID,
        call_kind: str,
        provider: str,
        cost_reserved_micros: int,
        id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> LlmUsageReservation:
        """Create a new reservation in 'reserved' state."""
        reservation = LlmUsageReservation(
            id=id or uuid4(),
            user_id=user_id,
            request_id=request_id,
            call_kind=call_kind,
            provider=provider,
            state="reserved",
            cost_reserved_micros=cost_reserved_micros,
            created_at=created_at or datetime.now(UTC),
        )
        self.db.add(reservation)
        self.db.flush()
        return reservation

    def get_reservation_by_id(self, reservation_id: UUID) -> LlmUsageReservation | None:
        """Fetch a reservation by primary key."""
        return self.db.get(LlmUsageReservation, reservation_id, populate_existing=True)

    def get_reservations_by_request_id(
        self, request_id: UUID
    ) -> list[LlmUsageReservation]:
        """Fetch all reservations for a given request_id in creation order."""
        statement = (
            select(LlmUsageReservation)
            .where(LlmUsageReservation.request_id == request_id)
            .order_by(LlmUsageReservation.created_at.asc())
        )
        return list(self.db.execute(statement).scalars().all())

    def get_reservations_by_user_id(self, user_id: UUID) -> list[LlmUsageReservation]:
        """Fetch all reservations for a given user_id in creation order."""
        statement = (
            select(LlmUsageReservation)
            .where(LlmUsageReservation.user_id == user_id)
            .order_by(LlmUsageReservation.created_at.asc())
        )
        return list(self.db.execute(statement).scalars().all())

    def transition_reservation_to_started(
        self, reservation_id: UUID, *, started_at: datetime | None = None
    ) -> bool:
        """Transition state from 'reserved' to 'started'. Idempotent."""
        now_ts = started_at or datetime.now(UTC)
        statement = (
            update(LlmUsageReservation)
            .where(
                LlmUsageReservation.id == reservation_id,
                LlmUsageReservation.state == "reserved",
            )
            .values(state="started", started_at=now_ts)
        )
        result = self.db.execute(statement)
        self.db.flush()
        updated = (result.rowcount or 0) > 0
        if updated:
            self._refresh_loaded_reservation(reservation_id)
        return updated

    def transition_reservation_to_settled(
        self, reservation_id: UUID, *, closed_at: datetime | None = None
    ) -> bool:
        """Transition state from 'started' to 'settled'. Idempotent."""
        now_ts = closed_at or datetime.now(UTC)
        statement = (
            update(LlmUsageReservation)
            .where(
                LlmUsageReservation.id == reservation_id,
                LlmUsageReservation.state == "started",
            )
            .values(state="settled", closed_at=now_ts)
        )
        result = self.db.execute(statement)
        self.db.flush()
        updated = (result.rowcount or 0) > 0
        if updated:
            self._refresh_loaded_reservation(reservation_id)
        return updated

    def transition_reservation_to_released(
        self, reservation_id: UUID, *, closed_at: datetime | None = None
    ) -> int | None:
        """Atomically claim and transition state from 'reserved' to 'released'.

        Returns:
            cost_reserved_micros if claimed, or None if the reservation was
            not in 'reserved' state (preventing duplicate counter decrements).
        """
        now_ts = closed_at or datetime.now(UTC)
        statement = (
            update(LlmUsageReservation)
            .where(
                LlmUsageReservation.id == reservation_id,
                LlmUsageReservation.state == "reserved",
            )
            .values(state="released", closed_at=now_ts)
            .returning(LlmUsageReservation.cost_reserved_micros)
        )
        result = self.db.execute(statement)
        row = result.fetchone()
        self.db.flush()
        if row is not None:
            self._refresh_loaded_reservation(reservation_id)
        return row[0] if row else None

    def transition_reservation_to_unknown(
        self, reservation_id: UUID, *, closed_at: datetime | None = None
    ) -> bool:
        """Transition state from 'started' to 'unknown' (used during crash recovery)."""
        now_ts = closed_at or datetime.now(UTC)
        statement = (
            update(LlmUsageReservation)
            .where(
                LlmUsageReservation.id == reservation_id,
                LlmUsageReservation.state == "started",
            )
            .values(state="unknown", closed_at=now_ts)
        )
        result = self.db.execute(statement)
        self.db.flush()
        updated = (result.rowcount or 0) > 0
        if updated:
            self._refresh_loaded_reservation(reservation_id)
        return updated

    def get_stale_reservations(
        self, cutoff: datetime, limit: int = 100
    ) -> list[LlmUsageReservation]:
        """Fetch reservations in 'reserved' or 'started' state older than cutoff."""
        statement = (
            select(LlmUsageReservation)
            .where(
                LlmUsageReservation.state.in_(["reserved", "started"]),
                LlmUsageReservation.created_at <= cutoff,
            )
            .order_by(LlmUsageReservation.created_at.asc())
            .limit(limit)
        )
        return list(self.db.execute(statement).scalars().all())

    def get_recent_reservations(self, limit: int = 50) -> list[LlmUsageReservation]:
        """Fetch most recent reservations in reverse chronological order."""
        statement = (
            select(LlmUsageReservation)
            .order_by(LlmUsageReservation.created_at.desc())
            .limit(limit)
        )
        return list(self.db.execute(statement).scalars().all())

    # -------------------------------------------------------------------------
    # Ledger Events
    # -------------------------------------------------------------------------

    def record_event(
        self,
        *,
        user_id: UUID,
        request_id: UUID,
        reservation_id: UUID,
        call_kind: str,
        provider: str,
        status: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        estimated_cost_micros: int = 0,
        model_id: str | None = None,
        id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> LlmUsageEvent:
        """Append an event to the users.llm_usage_event ledger."""
        event = LlmUsageEvent(
            id=id or uuid4(),
            user_id=user_id,
            request_id=request_id,
            reservation_id=reservation_id,
            call_kind=call_kind,
            provider=provider,
            model_id=model_id,
            status=status,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_micros=estimated_cost_micros,
            created_at=created_at or datetime.now(UTC),
        )
        self.db.add(event)
        self.db.flush()
        return event

    def get_events_by_request_id(self, request_id: UUID) -> list[LlmUsageEvent]:
        """Fetch all events for a given request_id."""
        statement = (
            select(LlmUsageEvent)
            .where(LlmUsageEvent.request_id == request_id)
            .order_by(LlmUsageEvent.created_at.asc())
        )
        return list(self.db.execute(statement).scalars().all())

    def get_events_by_user_id(
        self, user_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[LlmUsageEvent]:
        """Fetch usage events for a user in reverse chronological order."""
        statement = (
            select(LlmUsageEvent)
            .where(LlmUsageEvent.user_id == user_id)
            .order_by(LlmUsageEvent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.execute(statement).scalars().all())

    def get_recent_events(self, limit: int = 50) -> list[LlmUsageEvent]:
        """Fetch most recent events in reverse chronological order."""
        statement = (
            select(LlmUsageEvent).order_by(LlmUsageEvent.created_at.desc()).limit(limit)
        )
        return list(self.db.execute(statement).scalars().all())

    # -------------------------------------------------------------------------
    # Quota Counters
    # -------------------------------------------------------------------------

    def _refresh_loaded_counter(
        self,
        *,
        scope: str,
        scope_key: str,
        window_kind: str,
        window_start: date,
    ) -> None:
        """Refresh an already-loaded counter object in the session identity map.

        When the session factory is configured with expire_on_commit=False,
        raw SQL statements (like reserve_quota's UPSERT) or bulk updates do not
        automatically update or expire ORM entities already loaded into memory.
        This helper ensures any retained LlmUsageCounter instance immediately
        reflects database state.
        """
        ident = (scope, scope_key, window_kind, window_start)
        key = inspect(LlmUsageCounter).identity_key_from_primary_key(ident)
        obj = self.db.identity_map.get(key)
        if obj is not None:
            self.db.refresh(obj)

    def reserve_quota(
        self,
        *,
        scope: str,
        scope_key: str,
        window_kind: str,
        window_start: date,
        call_cost: int,
        cost_reserved_micros: int,
        call_limit: int | None = None,
        cost_limit_micros: int | None = None,
    ) -> int | None:
        """Atomically check and reserve quota in users.llm_usage_counter.

        Returns:
            New calls_reserved total if the reservation was admitted,
            or None if a limit would be exceeded.
        """
        statement = text(
            """
            INSERT INTO users.llm_usage_counter AS c
                   (scope, scope_key, window_kind, window_start,
                    calls_reserved, calls_settled, cost_reserved_micros, cost_micros)
            SELECT :scope, :key, :window_kind, :window_start, :cost, 0, :cost_micros, 0
             WHERE (:call_limit IS NULL OR :cost <= :call_limit)
               AND (:cost_limit_micros IS NULL OR :cost_micros <= :cost_limit_micros)
            ON CONFLICT (scope, scope_key, window_kind, window_start) DO UPDATE
               SET calls_reserved       = c.calls_reserved + :cost,
                    cost_reserved_micros = c.cost_reserved_micros + :cost_micros
             WHERE (:call_limit IS NULL OR c.calls_reserved + :cost <= :call_limit)
               AND (:cost_limit_micros IS NULL OR c.cost_micros + c.cost_reserved_micros + :cost_micros <= :cost_limit_micros)
            RETURNING calls_reserved;
            """
        )
        result = self.db.execute(
            statement,
            {
                "scope": scope,
                "key": scope_key,
                "window_kind": window_kind,
                "window_start": window_start,
                "cost": call_cost,
                "cost_micros": cost_reserved_micros,
                "call_limit": call_limit,
                "cost_limit_micros": cost_limit_micros,
            },
        )
        row = result.fetchone()
        self.db.flush()
        if row is not None:
            self._refresh_loaded_counter(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
            )
        return row[0] if row else None

    def settle_quota(
        self,
        *,
        scope: str,
        scope_key: str,
        window_kind: str,
        window_start: date,
        cost_reserved_micros: int,
        settled_cost_micros: int,
    ) -> None:
        """Move cost from reserved to settled and increment calls_settled."""
        statement = (
            update(LlmUsageCounter)
            .where(
                LlmUsageCounter.scope == scope,
                LlmUsageCounter.scope_key == scope_key,
                LlmUsageCounter.window_kind == window_kind,
                LlmUsageCounter.window_start == window_start,
            )
            .values(
                calls_settled=LlmUsageCounter.calls_settled + 1,
                cost_reserved_micros=LlmUsageCounter.cost_reserved_micros
                - cost_reserved_micros,
                cost_micros=LlmUsageCounter.cost_micros + settled_cost_micros,
            )
        )
        self.db.execute(statement)
        self.db.flush()
        self._refresh_loaded_counter(
            scope=scope,
            scope_key=scope_key,
            window_kind=window_kind,
            window_start=window_start,
        )

    def release_quota(
        self,
        *,
        scope: str,
        scope_key: str,
        window_kind: str,
        window_start: date,
        cost_reserved_micros: int,
        call_count: int = 1,
    ) -> None:
        """Deduct released calls and cost from reserved quota counters."""
        statement = (
            update(LlmUsageCounter)
            .where(
                LlmUsageCounter.scope == scope,
                LlmUsageCounter.scope_key == scope_key,
                LlmUsageCounter.window_kind == window_kind,
                LlmUsageCounter.window_start == window_start,
            )
            .values(
                calls_reserved=LlmUsageCounter.calls_reserved - call_count,
                cost_reserved_micros=LlmUsageCounter.cost_reserved_micros
                - cost_reserved_micros,
            )
        )
        self.db.execute(statement)
        self.db.flush()
        self._refresh_loaded_counter(
            scope=scope,
            scope_key=scope_key,
            window_kind=window_kind,
            window_start=window_start,
        )

    def get_counter(
        self,
        *,
        scope: str,
        scope_key: str,
        window_kind: str,
        window_start: date,
    ) -> LlmUsageCounter | None:
        """Fetch counter row by primary key."""
        return self.db.get(
            LlmUsageCounter,
            (scope, scope_key, window_kind, window_start),
            populate_existing=True,
        )

    def get_counters_by_scope(
        self, scope: str, scope_key: str
    ) -> list[LlmUsageCounter]:
        """Fetch all counter rows for a given scope and scope_key."""
        statement = (
            select(LlmUsageCounter)
            .where(
                LlmUsageCounter.scope == scope,
                LlmUsageCounter.scope_key == scope_key,
            )
            .order_by(LlmUsageCounter.window_start.desc())
        )
        return list(self.db.execute(statement).scalars().all())
