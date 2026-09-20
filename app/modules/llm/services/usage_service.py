"""LLM usage controls and quota reservation service (Roadmap Step 8e-2).

Enforces reserve-then-call quota constraints across daily and monthly windows for both
user and global scopes. Manages the short-transaction lifecycle:
  1. reserve -> COMMIT
  2. start -> COMMIT
  3. provider converse
  4. settle / release -> COMMIT

Reference: api/docs/llm-usage-controls.md §5, §6, §7, §8, §10
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import math
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, settings as app_settings
from app.modules.auth.schemas.user_schemas import CurrentUser
from app.modules.llm.repositories import LlmUsageRepository
from app.modules.llm.schemas.llm_schemas import (
    LlmAdminUsageResponse,
    LlmEventSummary,
    LlmQuotaWindowView,
    LlmReservationSummary,
    LlmUserUsageResponse,
)
from app.modules.llm.services.errors import (
    CodeValidationError,
    LlmProviderNotConfiguredError,
    LlmQuotaExceededError,
    LlmServiceError,
)
from app.modules.llm.services.provider import LlmProvider, LlmProviderResponse

BYTES_PER_TOKEN_ESTIMATE = 2


def prompt_tokens_estimate(byte_length: int) -> int:
    """Calculate pessimistic token count from byte length."""
    return max(1, math.ceil(byte_length / BYTES_PER_TOKEN_ESTIMATE))


def calculate_cost_micros(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    prompt_price_per_1k: int,
    completion_price_per_1k: int,
) -> int:
    """Calculate cost in micros based on token counts and price per 1k tokens."""
    prompt_cost = math.ceil(prompt_tokens * prompt_price_per_1k / 1000)
    completion_cost = math.ceil(completion_tokens * completion_price_per_1k / 1000)
    return prompt_cost + completion_cost


class LlmUsageService:
    """Manages LLM quota reservation, state transitions, and ledger logging."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or app_settings

    # -------------------------------------------------------------------------
    # Window and Scope Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def get_window_dates(now: datetime | None = None) -> tuple[date, date]:
        """Return (utc_day_start, utc_month_start) for the current UTC instant."""
        if now is None:
            now_ts = datetime.now(UTC)
        elif now.tzinfo is None:
            now_ts = now.replace(tzinfo=UTC)
        else:
            now_ts = now.astimezone(UTC)
        day_start = now_ts.date()
        month_start = date(now_ts.year, now_ts.month, 1)
        return day_start, month_start

    @staticmethod
    def calculate_resets_at(window_kind: str, now: datetime | None = None) -> datetime:
        """Calculate next UTC window boundary datetime."""
        if now is None:
            now_ts = datetime.now(UTC)
        elif now.tzinfo is None:
            now_ts = now.replace(tzinfo=UTC)
        else:
            now_ts = now.astimezone(UTC)
        if window_kind == "day":
            return datetime(
                now_ts.year, now_ts.month, now_ts.day, tzinfo=UTC
            ) + timedelta(days=1)
        if now_ts.month == 12:
            return datetime(now_ts.year + 1, 1, 1, tzinfo=UTC)
        return datetime(now_ts.year, now_ts.month + 1, 1, tzinfo=UTC)

    @classmethod
    def calculate_retry_after(
        cls, window_kind: str, now: datetime | None = None
    ) -> int:
        """Calculate seconds to the next UTC window boundary."""
        if now is None:
            now_ts = datetime.now(UTC)
        elif now.tzinfo is None:
            now_ts = now.replace(tzinfo=UTC)
        else:
            now_ts = now.astimezone(UTC)
        resets_at = cls.calculate_resets_at(window_kind, now_ts)
        return max(1, int((resets_at - now_ts).total_seconds()))

    def resolve_user_limits(
        self,
        user_id: UUID,
        email: str | None,
        db: Session,
    ) -> tuple[int, int]:
        """Resolve daily and monthly call limits for a user based on entitlement or tier."""
        repo = LlmUsageRepository(db)
        entitlement = repo.get_entitlement(user_id)
        now_ts = datetime.now(UTC)

        if entitlement is not None:
            is_valid = (
                entitlement.valid_until is None or entitlement.valid_until > now_ts
            )
            if is_valid:
                tier_daily = (
                    self.settings.llm_dev_tier_daily_calls
                    if entitlement.tier == "developer"
                    else self.settings.llm_free_tier_daily_calls
                )
                tier_monthly = (
                    self.settings.llm_dev_tier_monthly_calls
                    if entitlement.tier == "developer"
                    else self.settings.llm_free_tier_monthly_calls
                )
                daily = (
                    entitlement.daily_call_limit
                    if entitlement.daily_call_limit is not None
                    else tier_daily
                )
                monthly = (
                    entitlement.monthly_call_limit
                    if entitlement.monthly_call_limit is not None
                    else tier_monthly
                )
                return daily, monthly

        normalized_email = (email or "").strip().lower()
        if normalized_email and normalized_email in self.settings.llm_allowed_email_set:
            return (
                self.settings.llm_dev_tier_daily_calls,
                self.settings.llm_dev_tier_monthly_calls,
            )

        return (
            self.settings.llm_free_tier_daily_calls,
            self.settings.llm_free_tier_monthly_calls,
        )

    # -------------------------------------------------------------------------
    # Cost Bound Calculations
    # -------------------------------------------------------------------------

    def compute_guard_cost_bound(self, prompt_bytes: int | None = None) -> int:
        """Compute conservative upper-bound cost for the safety guard call."""
        raw_bytes = (
            prompt_bytes
            if prompt_bytes is not None
            else self.settings.llm_max_total_bytes
        )
        input_tokens = (
            prompt_tokens_estimate(raw_bytes)
            + self.settings.llm_system_prompt_allowance_tokens
        )
        output_tokens = self.settings.llm_guard_output_tokens_max
        return calculate_cost_micros(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            prompt_price_per_1k=self.settings.llm_worst_case_price_micros_prompt,
            completion_price_per_1k=self.settings.llm_worst_case_price_micros_completion,
        )

    def compute_generator_cost_bound(self, prompt_bytes: int) -> int:
        """Compute conservative upper-bound cost for the generator call."""
        input_tokens = (
            prompt_tokens_estimate(prompt_bytes)
            + self.settings.llm_system_prompt_allowance_tokens
        )
        output_tokens = self.settings.llm_max_tokens
        return calculate_cost_micros(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            prompt_price_per_1k=self.settings.llm_worst_case_price_micros_prompt,
            completion_price_per_1k=self.settings.llm_worst_case_price_micros_completion,
        )

    def compute_repair_cost_bound(self, repair_prompt: str) -> int:
        """Compute conservative upper-bound cost for a syntax repair attempt."""
        prompt_bytes = len(repair_prompt.encode("utf-8"))
        input_tokens = (
            prompt_tokens_estimate(prompt_bytes)
            + self.settings.llm_system_prompt_allowance_tokens
        )
        output_tokens = self.settings.llm_max_tokens
        return calculate_cost_micros(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            prompt_price_per_1k=self.settings.llm_worst_case_price_micros_prompt,
            completion_price_per_1k=self.settings.llm_worst_case_price_micros_completion,
        )

    # -------------------------------------------------------------------------
    # Reservation Lifecycle
    # -------------------------------------------------------------------------

    def reserve_initial_generation(
        self,
        *,
        user: CurrentUser,
        request_id: UUID,
        provider: str,
        prompt_bytes: int,
        guard_prompt_bytes: int | None = None,
    ) -> tuple[UUID, UUID]:
        """Reserve quota for guard and generator calls (call_cost=2).

        Acquires counter rows in canonical deadlock-free lock order:
          global/day -> global/month -> user/day -> user/month

        Returns:
            (guard_reservation_id, generator_reservation_id)
        """
        guard_bound = self.compute_guard_cost_bound(guard_prompt_bytes)
        generator_bound = self.compute_generator_cost_bound(prompt_bytes)
        total_bound = guard_bound + generator_bound

        now = datetime.now(UTC)
        day_start, month_start = self.get_window_dates(now)
        user_id_str = str(user.id)

        with self.session_factory() as session:
            repo = LlmUsageRepository(session)
            user_daily_limit, user_monthly_limit = self.resolve_user_limits(
                user.id, user.email, session
            )

            # Sequence of 4 reservations in canonical deadlock-free order:
            targets = [
                (
                    "global",
                    "-",
                    "day",
                    day_start,
                    2,
                    total_bound,
                    self.settings.llm_global_daily_calls,
                    None,
                ),
                (
                    "global",
                    "-",
                    "month",
                    month_start,
                    2,
                    total_bound,
                    None,
                    self.settings.llm_global_monthly_cost_ceiling_micros,
                ),
                (
                    "user",
                    user_id_str,
                    "day",
                    day_start,
                    2,
                    total_bound,
                    user_daily_limit,
                    None,
                ),
                (
                    "user",
                    user_id_str,
                    "month",
                    month_start,
                    2,
                    total_bound,
                    user_monthly_limit,
                    None,
                ),
            ]

            for (
                scope,
                scope_key,
                window_kind,
                window_start,
                call_cost,
                cost_micros,
                call_lim,
                cost_lim,
            ) in targets:
                admitted = repo.reserve_quota(
                    scope=scope,
                    scope_key=scope_key,
                    window_kind=window_kind,
                    window_start=window_start,
                    call_cost=call_cost,
                    cost_reserved_micros=cost_micros,
                    call_limit=call_lim,
                    cost_limit_micros=cost_lim,
                )
                if admitted is None:
                    session.rollback()
                    retry_after = self.calculate_retry_after(window_kind)
                    if scope == "user":
                        message = (
                            "Daily quota exceeded for user"
                            if window_kind == "day"
                            else "Monthly quota exceeded for user"
                        )
                    else:
                        message = (
                            "Daily global quota exceeded"
                            if window_kind == "day"
                            else "Monthly global cost ceiling exceeded"
                        )
                    raise LlmQuotaExceededError(
                        message=message,
                        retry_after=retry_after,
                        scope=scope,
                        window_kind=window_kind,
                    )

            guard_res = repo.create_reservation(
                user_id=user.id,
                request_id=request_id,
                call_kind="guard",
                provider=provider,
                cost_reserved_micros=guard_bound,
                created_at=now,
            )
            gen_res = repo.create_reservation(
                user_id=user.id,
                request_id=request_id,
                call_kind="generator",
                provider=provider,
                cost_reserved_micros=generator_bound,
                created_at=now,
            )
            session.commit()
            return guard_res.id, gen_res.id

    def reserve_repair_call(
        self,
        *,
        user: CurrentUser,
        request_id: UUID,
        provider: str,
        repair_prompt: str,
    ) -> UUID:
        """Reserve 1 repair call (call_cost=1) with cost bounded from actual repair prompt.

        If quota is exceeded, rolls back and immediately raises CodeValidationError
        per §5.1 without returning invalid code.
        """
        repair_bound = self.compute_repair_cost_bound(repair_prompt)
        now = datetime.now(UTC)
        day_start, month_start = self.get_window_dates(now)
        user_id_str = str(user.id)

        with self.session_factory() as session:
            repo = LlmUsageRepository(session)
            user_daily_limit, user_monthly_limit = self.resolve_user_limits(
                user.id, user.email, session
            )

            targets = [
                (
                    "global",
                    "-",
                    "day",
                    day_start,
                    1,
                    repair_bound,
                    self.settings.llm_global_daily_calls,
                    None,
                ),
                (
                    "global",
                    "-",
                    "month",
                    month_start,
                    1,
                    repair_bound,
                    None,
                    self.settings.llm_global_monthly_cost_ceiling_micros,
                ),
                (
                    "user",
                    user_id_str,
                    "day",
                    day_start,
                    1,
                    repair_bound,
                    user_daily_limit,
                    None,
                ),
                (
                    "user",
                    user_id_str,
                    "month",
                    month_start,
                    1,
                    repair_bound,
                    user_monthly_limit,
                    None,
                ),
            ]

            for (
                scope,
                scope_key,
                window_kind,
                window_start,
                call_cost,
                cost_micros,
                call_lim,
                cost_lim,
            ) in targets:
                admitted = repo.reserve_quota(
                    scope=scope,
                    scope_key=scope_key,
                    window_kind=window_kind,
                    window_start=window_start,
                    call_cost=call_cost,
                    cost_reserved_micros=cost_micros,
                    call_limit=call_lim,
                    cost_limit_micros=cost_lim,
                )
                if admitted is None:
                    session.rollback()
                    raise CodeValidationError(
                        "Generated code did not pass syntax validation"
                    )

            repair_res = repo.create_reservation(
                user_id=user.id,
                request_id=request_id,
                call_kind="repair",
                provider=provider,
                cost_reserved_micros=repair_bound,
                created_at=now,
            )
            session.commit()
            return repair_res.id

    def start_call(
        self,
        *,
        reservation_id: UUID,
        provider: LlmProvider,
        model_id: str,
        user_id: UUID,
    ) -> None:
        """Run preflight check and transition reservation from 'reserved' to 'started'.

        If preflight fails, releases this reservation in a separate transaction and re-raises.
        """
        try:
            provider.preflight(model_id=model_id)
        except LlmProviderNotConfiguredError:
            self.release_reservation(reservation_id=reservation_id, user_id=user_id)
            raise

        with self.session_factory() as session:
            repo = LlmUsageRepository(session)
            started = repo.transition_reservation_to_started(reservation_id)
            if not started:
                session.rollback()
                raise LlmServiceError(
                    "Reservation cannot be started (already closed, released, or started)",
                    code="llm_internal",
                    status_code=500,
                )
            session.commit()

    def settle_call(
        self,
        *,
        reservation_id: UUID,
        user_id: UUID,
        request_id: UUID,
        call_kind: str,
        provider_name: str,
        model_id: str | None,
        response: LlmProviderResponse | None,
        status: str,
    ) -> None:
        """Transition reservation 'started' -> 'settled', write ledger event and settle counter.

        Evaluates token completeness: if complete and valid, settles the estimated cost.
        If error, timeout, or partial/missing usage tokens, retains full conservative bound.
        """
        user_id_str = str(user_id)

        with self.session_factory() as session:
            repo = LlmUsageRepository(session)
            res = repo.get_reservation_by_id(reservation_id)
            if res is None:
                session.rollback()
                return

            # F1: Window dates are bound to the captured reservation instant!
            day_start, month_start = self.get_window_dates(res.created_at)

            cost_reserved = res.cost_reserved_micros
            prompt_tokens = response.prompt_tokens if response else 0
            completion_tokens = response.completion_tokens if response else 0

            # Settle cost evaluation:
            # Settle replaces bound ONLY when status == 'ok' AND token usage is complete (> 0).
            if status == "ok" and prompt_tokens > 0 and completion_tokens > 0:
                settled_cost = calculate_cost_micros(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    prompt_price_per_1k=self.settings.llm_worst_case_price_micros_prompt,
                    completion_price_per_1k=self.settings.llm_worst_case_price_micros_completion,
                )
            else:
                # Full bound kept on errors, timeouts, and missing/partial usage per §5.3
                settled_cost = cost_reserved

            # Transition reservation to settled
            transitioned = repo.transition_reservation_to_settled(reservation_id)
            if not transitioned:
                # Idempotent: already settled
                session.rollback()
                return

            # Append event to ledger
            repo.record_event(
                user_id=user_id,
                request_id=request_id,
                reservation_id=reservation_id,
                call_kind=call_kind,
                provider=provider_name,
                model_id=model_id,
                status=status,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_micros=settled_cost,
            )

            # Update all 4 counter rows in canonical order:
            scopes = [
                ("global", "-", "day", day_start),
                ("global", "-", "month", month_start),
                ("user", user_id_str, "day", day_start),
                ("user", user_id_str, "month", month_start),
            ]
            for scope, scope_key, window_kind, window_start in scopes:
                repo.settle_quota(
                    scope=scope,
                    scope_key=scope_key,
                    window_kind=window_kind,
                    window_start=window_start,
                    cost_reserved_micros=cost_reserved,
                    settled_cost_micros=settled_cost,
                )

            session.commit()

    def release_reservation(self, *, reservation_id: UUID, user_id: UUID) -> None:
        """Atomically release an open 'reserved' reservation and decrement counters."""
        user_id_str = str(user_id)

        with self.session_factory() as session:
            repo = LlmUsageRepository(session)
            res = repo.get_reservation_by_id(reservation_id)
            if res is None:
                session.rollback()
                return

            # F1: Window dates are bound to the captured reservation instant!
            day_start, month_start = self.get_window_dates(res.created_at)

            # Step 1: Atomic state claim
            returned_bound = repo.transition_reservation_to_released(reservation_id)
            if returned_bound is None:
                # Already transitioned or closed
                session.rollback()
                return

            # Step 2: Decrement counters across all 4 rows in canonical order
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

    def release_downstream_reservations(
        self,
        *,
        request_id: UUID,
        user_id: UUID,
    ) -> None:
        """Release all reservations for this request_id that remain in 'reserved' state."""
        with self.session_factory() as session:
            repo = LlmUsageRepository(session)
            reservations = repo.get_reservations_by_request_id(request_id)
            reserved_ids = [r.id for r in reservations if r.state == "reserved"]

        for res_id in reserved_ids:
            self.release_reservation(reservation_id=res_id, user_id=user_id)

    # -------------------------------------------------------------------------
    # Usage Views (Roadmap Step 8e-3)
    # -------------------------------------------------------------------------

    def get_user_usage(
        self,
        user: CurrentUser,
        now: datetime | None = None,
    ) -> LlmUserUsageResponse:
        """Fetch caller's current usage counters, active limits, and reset times."""
        now_ts = now or datetime.now(UTC)
        if now_ts.tzinfo is None:
            now_ts = now_ts.replace(tzinfo=UTC)
        else:
            now_ts = now_ts.astimezone(UTC)

        day_start, month_start = self.get_window_dates(now_ts)
        user_id_str = str(user.id)

        with self.session_factory() as session:
            repo = LlmUsageRepository(session)
            entitlement = repo.get_entitlement(user.id)
            user_daily_limit, user_monthly_limit = self.resolve_user_limits(
                user.id, user.email, session
            )

            tier = "free"
            if entitlement is not None:
                is_valid = (
                    entitlement.valid_until is None or entitlement.valid_until > now_ts
                )
                if is_valid:
                    tier = entitlement.tier
                elif (
                    user.email
                    and user.email.strip().lower()
                    in self.settings.llm_allowed_email_set
                ):
                    tier = "developer"
            elif (
                user.email
                and user.email.strip().lower() in self.settings.llm_allowed_email_set
            ):
                tier = "developer"

            day_counter = repo.get_counter(
                scope="user",
                scope_key=user_id_str,
                window_kind="day",
                window_start=day_start,
            )
            month_counter = repo.get_counter(
                scope="user",
                scope_key=user_id_str,
                window_kind="month",
                window_start=month_start,
            )

        day_resets_at = self.calculate_resets_at("day", now_ts)
        month_resets_at = self.calculate_resets_at("month", now_ts)

        day_view = LlmQuotaWindowView(
            scope="user",
            window_kind="day",
            window_start=day_start,
            calls_reserved=day_counter.calls_reserved if day_counter else 0,
            calls_settled=day_counter.calls_settled if day_counter else 0,
            # In the Step 8e quota model (§4.3, §5.1), calls_reserved tracks all calls admitted
            # into the window and is never decremented on settle. calls_settled tracks completed
            # calls. The total calls counted against quota is therefore calls_reserved.
            calls_total=day_counter.calls_reserved if day_counter else 0,
            call_limit=user_daily_limit,
            cost_reserved_micros=day_counter.cost_reserved_micros if day_counter else 0,
            cost_micros=day_counter.cost_micros if day_counter else 0,
            cost_total_micros=(
                (day_counter.cost_reserved_micros + day_counter.cost_micros)
                if day_counter
                else 0
            ),
            cost_limit_micros=None,
            resets_at=day_resets_at,
            retry_after=self.calculate_retry_after("day", now_ts),
        )

        month_view = LlmQuotaWindowView(
            scope="user",
            window_kind="month",
            window_start=month_start,
            calls_reserved=month_counter.calls_reserved if month_counter else 0,
            calls_settled=month_counter.calls_settled if month_counter else 0,
            calls_total=month_counter.calls_reserved if month_counter else 0,
            call_limit=user_monthly_limit,
            cost_reserved_micros=month_counter.cost_reserved_micros
            if month_counter
            else 0,
            cost_micros=month_counter.cost_micros if month_counter else 0,
            cost_total_micros=(
                (month_counter.cost_reserved_micros + month_counter.cost_micros)
                if month_counter
                else 0
            ),
            cost_limit_micros=None,
            resets_at=month_resets_at,
            retry_after=self.calculate_retry_after("month", now_ts),
        )

        return LlmUserUsageResponse(
            user_id=user.id,
            tier=tier,
            day=day_view,
            month=month_view,
        )

    def get_admin_usage(
        self,
        now: datetime | None = None,
    ) -> LlmAdminUsageResponse:
        """Fetch global usage counters, configured ceilings, and recent activity."""
        now_ts = now or datetime.now(UTC)
        if now_ts.tzinfo is None:
            now_ts = now_ts.replace(tzinfo=UTC)
        else:
            now_ts = now_ts.astimezone(UTC)

        day_start, month_start = self.get_window_dates(now_ts)

        with self.session_factory() as session:
            repo = LlmUsageRepository(session)
            global_day_counter = repo.get_counter(
                scope="global",
                scope_key="-",
                window_kind="day",
                window_start=day_start,
            )
            global_month_counter = repo.get_counter(
                scope="global",
                scope_key="-",
                window_kind="month",
                window_start=month_start,
            )
            recent_res_models = repo.get_recent_reservations(limit=50)
            recent_event_models = repo.get_recent_events(limit=50)

            recent_reservations = [
                LlmReservationSummary(
                    id=r.id,
                    user_id=r.user_id,
                    request_id=r.request_id,
                    call_kind=r.call_kind,
                    provider=r.provider,
                    state=r.state,
                    cost_reserved_micros=r.cost_reserved_micros,
                    created_at=r.created_at,
                    started_at=r.started_at,
                    closed_at=r.closed_at,
                )
                for r in recent_res_models
            ]
            recent_events = [
                LlmEventSummary(
                    id=e.id,
                    user_id=e.user_id,
                    request_id=e.request_id,
                    reservation_id=e.reservation_id,
                    call_kind=e.call_kind,
                    provider=e.provider,
                    model_id=e.model_id,
                    status=e.status,
                    prompt_tokens=e.prompt_tokens,
                    completion_tokens=e.completion_tokens,
                    estimated_cost_micros=e.estimated_cost_micros,
                    created_at=e.created_at,
                )
                for e in recent_event_models
            ]

        day_resets_at = self.calculate_resets_at("day", now_ts)
        month_resets_at = self.calculate_resets_at("month", now_ts)

        global_day_view = LlmQuotaWindowView(
            scope="global",
            window_kind="day",
            window_start=day_start,
            calls_reserved=global_day_counter.calls_reserved
            if global_day_counter
            else 0,
            calls_settled=global_day_counter.calls_settled if global_day_counter else 0,
            calls_total=(
                global_day_counter.calls_reserved if global_day_counter else 0
            ),
            call_limit=self.settings.llm_global_daily_calls,
            cost_reserved_micros=global_day_counter.cost_reserved_micros
            if global_day_counter
            else 0,
            cost_micros=global_day_counter.cost_micros if global_day_counter else 0,
            cost_total_micros=(
                (
                    global_day_counter.cost_reserved_micros
                    + global_day_counter.cost_micros
                )
                if global_day_counter
                else 0
            ),
            cost_limit_micros=None,
            resets_at=day_resets_at,
            retry_after=self.calculate_retry_after("day", now_ts),
        )

        global_month_view = LlmQuotaWindowView(
            scope="global",
            window_kind="month",
            window_start=month_start,
            calls_reserved=global_month_counter.calls_reserved
            if global_month_counter
            else 0,
            calls_settled=global_month_counter.calls_settled
            if global_month_counter
            else 0,
            calls_total=(
                global_month_counter.calls_reserved if global_month_counter else 0
            ),
            call_limit=None,
            cost_reserved_micros=global_month_counter.cost_reserved_micros
            if global_month_counter
            else 0,
            cost_micros=global_month_counter.cost_micros if global_month_counter else 0,
            cost_total_micros=(
                (
                    global_month_counter.cost_reserved_micros
                    + global_month_counter.cost_micros
                )
                if global_month_counter
                else 0
            ),
            cost_limit_micros=self.settings.llm_global_monthly_cost_ceiling_micros,
            resets_at=month_resets_at,
            retry_after=self.calculate_retry_after("month", now_ts),
        )

        return LlmAdminUsageResponse(
            global_day=global_day_view,
            global_month=global_month_view,
            recent_reservations=recent_reservations,
            recent_events=recent_events,
        )
