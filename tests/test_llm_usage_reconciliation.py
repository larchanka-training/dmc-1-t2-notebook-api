"""Database tests for stale LLM reservation reconciliation (Roadmap Step 8e-3).

Verifies the 3 required database tests from project/api/docs/llm-usage-controls.md §10:
1. stale 'reserved' reconciliation: transitions to 'released', decrements both calls
   and cost columns across all four counter rows.
2. stale 'started' reconciliation: transitions to 'unknown', counters unchanged,
   appends exactly one ledger row with status='unknown', call_kind preserved, model_id=None.
3. reconciliation vs late settlement race: deterministic concurrency without double-counting
   or double-releasing.
"""

from datetime import UTC, datetime, timedelta
import threading
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.modules.auth.models.user import User as UserModel
from app.modules.auth.schemas import CurrentUser
from app.modules.llm.repositories import LlmUsageRepository
from app.modules.llm.services.provider import LlmProviderResponse
from app.modules.llm.services.reconciliation_service import LlmReconciliationService
from app.modules.llm.services.usage_service import (
    LlmUsageService,
    calculate_cost_micros,
)


def _create_user(db: Session) -> tuple[UserModel, CurrentUser]:
    user = UserModel(
        id=uuid4(),
        email=f"user-{uuid4()}@notebook.local",
        display_name="Reconciliation User",
        created_at=datetime.now(UTC),
    )
    db.add(user)
    db.commit()
    current_user = CurrentUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        roles=[],
    )
    return user, current_user


def _make_test_settings(**kwargs: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "app_env": "dev",
        "jwt_secret": "x" * 32,
        "otp_hash_secret": "y" * 32,
        "llm_provider": "openrouter",
        "llm_openrouter_api_key": "test-key-12345",
        "llm_worst_case_price_micros_prompt": 3000,
        "llm_worst_case_price_micros_completion": 15000,
        "llm_global_daily_calls": 1000,
        "llm_global_monthly_cost_ceiling_micros": 100_000_000,
        "llm_reconciliation_stale_seconds": 300,
        "llm_reconciliation_batch_size": 100,
    }
    defaults.update(kwargs)
    return Settings(**defaults)


# -----------------------------------------------------------------------------
# Test 1: Stale 'reserved' reconciliation
# -----------------------------------------------------------------------------


def test_1_stale_reserved_reconciliation(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """1. Stale 'reserved' row transitions to 'released', returning both columns on all 4 counters."""
    settings = _make_test_settings(llm_reconciliation_stale_seconds=300)
    usage_svc = LlmUsageService(session_factory=pg_session_factory, settings=settings)
    reconciler = LlmReconciliationService(
        session_factory=pg_session_factory, settings=settings
    )

    now = datetime.now(UTC)
    stale_created_at = now - timedelta(minutes=10)
    day_start, month_start = usage_svc.get_window_dates(stale_created_at)

    with pg_session_factory() as session:
        user_model, _ = _create_user(session)
        user_id_str = str(user_model.id)
        repo = LlmUsageRepository(session)

        # Create a stale reservation in 'reserved' state
        cost_reserved = 45000
        req_id = uuid4()
        res = repo.create_reservation(
            user_id=user_model.id,
            request_id=req_id,
            call_kind="generator",
            provider="openrouter",
            cost_reserved_micros=cost_reserved,
            created_at=stale_created_at,
        )
        res_id = res.id

        # Pre-seed quota across all 4 counter rows
        scopes = [
            ("global", "-", "day", day_start),
            ("global", "-", "month", month_start),
            ("user", user_id_str, "day", day_start),
            ("user", user_id_str, "month", month_start),
        ]
        for scope, scope_key, window_kind, window_start in scopes:
            repo.reserve_quota(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
                call_cost=1,
                cost_reserved_micros=cost_reserved,
            )
        session.commit()

    # Pre-condition check: counters are at 1 call and 45000 micros
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        for scope, scope_key, window_kind, window_start in scopes:
            ctr = repo.get_counter(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
            )
            assert ctr is not None
            assert ctr.calls_reserved == 1
            assert ctr.cost_reserved_micros == cost_reserved

    # Run reconciler
    summary = reconciler.reconcile_stale_reservations(now=now)

    assert summary.reconciled_reserved == 1
    assert summary.reconciled_started == 0
    assert summary.returned_cost_micros == cost_reserved

    # Verify database state
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        saved_res = repo.get_reservation_by_id(res_id)
        assert saved_res is not None
        assert saved_res.state == "released"
        assert saved_res.closed_at is not None

        # Verify BOTH calls_reserved and cost_reserved_micros decremented to 0 on all 4 counters
        for scope, scope_key, window_kind, window_start in scopes:
            ctr = repo.get_counter(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
            )
            assert ctr is not None, f"Counter missing for {scope}/{window_kind}"
            assert ctr.calls_reserved == 0, (
                f"calls_reserved not returned for {scope}/{window_kind}"
            )
            assert ctr.cost_reserved_micros == 0, (
                f"cost_reserved_micros not returned for {scope}/{window_kind}"
            )
            assert ctr.calls_settled == 0
            assert ctr.cost_micros == 0

        # Verify no ledger events written for released reservation
        events = repo.get_events_by_request_id(req_id)
        assert len(events) == 0


# -----------------------------------------------------------------------------
# Test 2: Stale 'started' reconciliation
# -----------------------------------------------------------------------------


def test_2_stale_started_reconciliation(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """2. Stale 'started' row transitions to 'unknown', counter unchanged, appends exactly one ledger row."""
    settings = _make_test_settings(llm_reconciliation_stale_seconds=300)
    usage_svc = LlmUsageService(session_factory=pg_session_factory, settings=settings)
    reconciler = LlmReconciliationService(
        session_factory=pg_session_factory, settings=settings
    )

    now = datetime.now(UTC)
    stale_created_at = now - timedelta(minutes=10)
    day_start, month_start = usage_svc.get_window_dates(stale_created_at)

    with pg_session_factory() as session:
        user_model, _ = _create_user(session)
        user_id_str = str(user_model.id)
        repo = LlmUsageRepository(session)

        # Create a stale reservation in 'started' state
        cost_reserved = 35000
        req_id = uuid4()
        res = repo.create_reservation(
            user_id=user_model.id,
            request_id=req_id,
            call_kind="guard",
            provider="openrouter",
            cost_reserved_micros=cost_reserved,
            created_at=stale_created_at,
        )
        repo.transition_reservation_to_started(
            res.id, started_at=stale_created_at + timedelta(seconds=1)
        )
        res_id = res.id

        # Pre-seed quota across all 4 counter rows
        scopes = [
            ("global", "-", "day", day_start),
            ("global", "-", "month", month_start),
            ("user", user_id_str, "day", day_start),
            ("user", user_id_str, "month", month_start),
        ]
        for scope, scope_key, window_kind, window_start in scopes:
            repo.reserve_quota(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
                call_cost=1,
                cost_reserved_micros=cost_reserved,
            )
        session.commit()

    # Run reconciler
    summary = reconciler.reconcile_stale_reservations(now=now)

    assert summary.reconciled_reserved == 0
    assert summary.reconciled_started == 1
    assert summary.returned_cost_micros == 0

    # Verify database state
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        saved_res = repo.get_reservation_by_id(res_id)
        assert saved_res is not None
        assert saved_res.state == "unknown"
        assert saved_res.closed_at is not None

        # Counters MUST BE UNCHANGED per §5.3 (stale started may have been served & billed)
        for scope, scope_key, window_kind, window_start in scopes:
            ctr = repo.get_counter(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
            )
            assert ctr is not None
            assert ctr.calls_reserved == 1
            assert ctr.cost_reserved_micros == cost_reserved
            assert ctr.calls_settled == 0
            assert ctr.cost_micros == 0

        # Exactly ONE ledger row appended
        events = repo.get_events_by_request_id(req_id)
        assert len(events) == 1
        event = events[0]
        assert event.reservation_id == res_id
        assert event.user_id == user_model.id
        assert event.call_kind == "guard"
        assert event.provider == "openrouter"
        assert event.model_id is None
        assert event.status == "unknown"
        assert event.prompt_tokens == 0
        assert event.completion_tokens == 0
        assert event.estimated_cost_micros == cost_reserved


# -----------------------------------------------------------------------------
# Test 3: Reconciliation vs late settlement race
# -----------------------------------------------------------------------------


def test_3_reconciliation_vs_late_settlement_race(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """3. Concurrent race between reconciliation and late settlement on a started reservation.

    Verifies that the state claim is strictly atomic:
    - Either late settlement wins (state -> 'settled', settled ledger, settle counters),
    - Or reconciliation wins (state -> 'unknown', unknown ledger, counters unchanged).
    - In neither case are counters corrupted or double-decremented.
    """
    settings = _make_test_settings(llm_reconciliation_stale_seconds=300)
    usage_svc = LlmUsageService(session_factory=pg_session_factory, settings=settings)
    reconciler = LlmReconciliationService(
        session_factory=pg_session_factory, settings=settings
    )

    now = datetime.now(UTC)
    stale_created_at = now - timedelta(minutes=10)
    day_start, month_start = usage_svc.get_window_dates(stale_created_at)

    with pg_session_factory() as session:
        user_model, _ = _create_user(session)
        user_id = user_model.id
        user_id_str = str(user_id)
        repo = LlmUsageRepository(session)

        cost_reserved = 50000
        req_id = uuid4()
        res = repo.create_reservation(
            user_id=user_id,
            request_id=req_id,
            call_kind="generator",
            provider="openrouter",
            cost_reserved_micros=cost_reserved,
            created_at=stale_created_at,
        )
        repo.transition_reservation_to_started(
            res.id, started_at=stale_created_at + timedelta(seconds=1)
        )
        res_id = res.id

        scopes = [
            ("global", "-", "day", day_start),
            ("global", "-", "month", month_start),
            ("user", user_id_str, "day", day_start),
            ("user", user_id_str, "month", month_start),
        ]
        for scope, scope_key, window_kind, window_start in scopes:
            repo.reserve_quota(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
                call_cost=1,
                cost_reserved_micros=cost_reserved,
            )
        session.commit()

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def task_reconcile() -> None:
        try:
            barrier.wait()
            reconciler.reconcile_stale_reservations(now=now)
        except Exception as exc:
            errors.append(exc)

    def task_late_settle() -> None:
        try:
            barrier.wait()
            provider_response = LlmProviderResponse(
                text="late response",
                model="openrouter/generator",
                prompt_tokens=20,
                completion_tokens=10,
            )
            usage_svc.settle_call(
                reservation_id=res_id,
                user_id=user_id,
                request_id=req_id,
                call_kind="generator",
                provider_name="openrouter",
                model_id="openrouter/generator",
                response=provider_response,
                status="ok",
            )
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=task_reconcile)
    t2 = threading.Thread(target=task_late_settle)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Errors during concurrency test: {errors}"

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        saved_res = repo.get_reservation_by_id(res_id)
        assert saved_res is not None
        assert saved_res.state in ("settled", "unknown")

        events = repo.get_events_by_request_id(req_id)
        # Exactly one winning transition wrote one ledger event
        assert len(events) == 1
        event = events[0]

        if saved_res.state == "settled":
            # Late settlement won
            assert event.status == "ok"
            assert event.model_id == "openrouter/generator"
            for scope, scope_key, window_kind, window_start in scopes:
                ctr = repo.get_counter(
                    scope=scope,
                    scope_key=scope_key,
                    window_kind=window_kind,
                    window_start=window_start,
                )
                assert ctr is not None
                assert ctr.calls_reserved == 1
                assert ctr.calls_settled == 1
                assert ctr.cost_reserved_micros == 0
                assert ctr.cost_micros > 0
        else:
            # Reconciliation won
            assert event.status == "unknown"
            assert event.model_id is None
            for scope, scope_key, window_kind, window_start in scopes:
                ctr = repo.get_counter(
                    scope=scope,
                    scope_key=scope_key,
                    window_kind=window_kind,
                    window_start=window_start,
                )
                assert ctr is not None
                assert ctr.calls_reserved == 1
                assert ctr.calls_settled == 0
                assert ctr.cost_reserved_micros == cost_reserved


# -----------------------------------------------------------------------------
# Additional checks: Dry-run and cutoff boundary
# -----------------------------------------------------------------------------


def test_reconciliation_dry_run_and_recent_reservation_untouched(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """Dry run counts stale reservations without mutating DB, and recent reservations remain open."""
    settings = _make_test_settings(llm_reconciliation_stale_seconds=300)
    reconciler = LlmReconciliationService(
        session_factory=pg_session_factory, settings=settings
    )

    now = datetime.now(UTC)
    stale_created = now - timedelta(minutes=10)
    recent_created = now - timedelta(seconds=10)  # Recent! Younger than 300s

    with pg_session_factory() as session:
        user_model, _ = _create_user(session)
        repo = LlmUsageRepository(session)

        stale_res = repo.create_reservation(
            user_id=user_model.id,
            request_id=uuid4(),
            call_kind="guard",
            provider="openrouter",
            cost_reserved_micros=12000,
            created_at=stale_created,
        )
        recent_res = repo.create_reservation(
            user_id=user_model.id,
            request_id=uuid4(),
            call_kind="generator",
            provider="openrouter",
            cost_reserved_micros=24000,
            created_at=recent_created,
        )
        stale_id = stale_res.id
        recent_id = recent_res.id
        session.commit()

    # Dry-run
    preview = reconciler.reconcile_stale_reservations(now=now, dry_run=True)
    assert preview.reconciled_reserved == 1
    assert preview.reconciled_started == 0
    assert preview.returned_cost_micros == 12000

    # Verify neither reservation was changed by dry-run
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        assert repo.get_reservation_by_id(stale_id).state == "reserved"
        assert repo.get_reservation_by_id(recent_id).state == "reserved"

    # Actual reconciliation
    actual = reconciler.reconcile_stale_reservations(now=now, dry_run=False)
    assert actual.reconciled_reserved == 1

    # Verify stale reservation is released, but recent reservation remains reserved!
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        assert repo.get_reservation_by_id(stale_id).state == "released"
        assert repo.get_reservation_by_id(recent_id).state == "reserved"


def test_reconcile_service_rejects_non_positive_threshold_and_limit(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """F3 Regression: Service strictly rejects non-positive stale_seconds and limit."""
    settings = _make_test_settings()
    reconciler = LlmReconciliationService(
        session_factory=pg_session_factory, settings=settings
    )

    for invalid_val in [0, -1, -300]:
        try:
            reconciler.reconcile_stale_reservations(stale_seconds=invalid_val)
            assert False, f"Expected ValueError for stale_seconds={invalid_val}"
        except ValueError as err:
            assert "stale_seconds must be a positive integer" in str(err)

    for invalid_limit in [0, -1, -100]:
        try:
            reconciler.reconcile_stale_reservations(limit=invalid_limit)
            assert False, f"Expected ValueError for limit={invalid_limit}"
        except ValueError as err:
            assert "limit must be a positive integer" in str(err)


def test_deterministic_interleaving_settle_first(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """S1: Deterministic interleaving where settlement claims state first.

    Reconciliation sees state is already 'settled' and performs zero mutations.
    """
    settings = _make_test_settings(llm_reconciliation_stale_seconds=300)
    usage_svc = LlmUsageService(session_factory=pg_session_factory, settings=settings)
    reconciler = LlmReconciliationService(
        session_factory=pg_session_factory, settings=settings
    )

    now = datetime.now(UTC)
    stale_created_at = now - timedelta(minutes=10)
    day_start, month_start = usage_svc.get_window_dates(stale_created_at)

    with pg_session_factory() as session:
        user_model, _ = _create_user(session)
        user_id = user_model.id
        repo = LlmUsageRepository(session)

        cost_reserved = 40000
        req_id = uuid4()
        res = repo.create_reservation(
            user_id=user_id,
            request_id=req_id,
            call_kind="generator",
            provider="openrouter",
            cost_reserved_micros=cost_reserved,
            created_at=stale_created_at,
        )
        repo.transition_reservation_to_started(res.id, started_at=stale_created_at)
        res_id = res.id

        scopes = [
            ("global", "-", "day", day_start),
            ("global", "-", "month", month_start),
            ("user", str(user_id), "day", day_start),
            ("user", str(user_id), "month", month_start),
        ]
        for scope, scope_key, window_kind, window_start in scopes:
            repo.reserve_quota(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
                call_cost=1,
                cost_reserved_micros=cost_reserved,
            )
        session.commit()

    # Step 1: Late settlement claims state first
    provider_response = LlmProviderResponse(
        text="settled text",
        model="openrouter/generator",
        prompt_tokens=50,
        completion_tokens=25,
    )
    usage_svc.settle_call(
        reservation_id=res_id,
        user_id=user_id,
        request_id=req_id,
        call_kind="generator",
        provider_name="openrouter",
        model_id="openrouter/generator",
        response=provider_response,
        status="ok",
    )

    # Step 2: Reconciler runs subsequently
    summary = reconciler.reconcile_stale_reservations(now=now)
    assert summary.reconciled_reserved == 0
    assert summary.reconciled_started == 0
    assert summary.returned_cost_micros == 0

    # Verification: reservation is 'settled', exactly 1 ledger event, counters reflect exact cost
    expected_cost = calculate_cost_micros(
        prompt_tokens=50,
        completion_tokens=25,
        prompt_price_per_1k=settings.llm_worst_case_price_micros_prompt,
        completion_price_per_1k=settings.llm_worst_case_price_micros_completion,
    )
    assert expected_cost > 0

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        assert repo.get_reservation_by_id(res_id).state == "settled"
        events = repo.get_events_by_request_id(req_id)
        assert len(events) == 1
        assert events[0].status == "ok"

        for scope, scope_key, window_kind, window_start in scopes:
            ctr = repo.get_counter(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
            )
            assert ctr is not None
            assert ctr.calls_reserved == 1
            assert ctr.calls_settled == 1
            assert ctr.cost_reserved_micros == 0
            assert ctr.cost_micros == expected_cost


def test_deterministic_interleaving_reconcile_first(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """S1: Sequential ordered execution where reconciliation claims state first.

    Reservation transitions to 'unknown', appending ledger event. Subsequent late settlement
    cannot overwrite 'unknown'. Counters across all four scopes preserve reserved cost.
    """
    settings = _make_test_settings(llm_reconciliation_stale_seconds=300)
    usage_svc = LlmUsageService(session_factory=pg_session_factory, settings=settings)
    reconciler = LlmReconciliationService(
        session_factory=pg_session_factory, settings=settings
    )

    now = datetime.now(UTC)
    stale_created_at = now - timedelta(minutes=10)
    day_start, month_start = usage_svc.get_window_dates(stale_created_at)

    with pg_session_factory() as session:
        user_model, _ = _create_user(session)
        user_id = user_model.id
        repo = LlmUsageRepository(session)

        cost_reserved = 40000
        req_id = uuid4()
        res = repo.create_reservation(
            user_id=user_id,
            request_id=req_id,
            call_kind="generator",
            provider="openrouter",
            cost_reserved_micros=cost_reserved,
            created_at=stale_created_at,
        )
        repo.transition_reservation_to_started(res.id, started_at=stale_created_at)
        res_id = res.id

        scopes = [
            ("global", "-", "day", day_start),
            ("global", "-", "month", month_start),
            ("user", str(user_id), "day", day_start),
            ("user", str(user_id), "month", month_start),
        ]
        for scope, scope_key, window_kind, window_start in scopes:
            repo.reserve_quota(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
                call_cost=1,
                cost_reserved_micros=cost_reserved,
            )
        session.commit()

    # Step 1: Reconciler runs first and claims 'started -> unknown'
    summary = reconciler.reconcile_stale_reservations(now=now)
    assert summary.reconciled_started == 1
    assert summary.returned_cost_micros == 0

    # Step 2: Late settlement attempts to run
    provider_response = LlmProviderResponse(
        text="late text",
        model="openrouter/generator",
        prompt_tokens=50,
        completion_tokens=25,
    )
    # settle_call handles atomic state transition; because state is already 'unknown',
    # it cannot claim 'started -> settled'
    usage_svc.settle_call(
        reservation_id=res_id,
        user_id=user_id,
        request_id=req_id,
        call_kind="generator",
        provider_name="openrouter",
        model_id="openrouter/generator",
        response=provider_response,
        status="ok",
    )

    # Verification: reservation remains 'unknown', exactly 1 ledger event ('call_reconciled_unknown')
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        assert repo.get_reservation_by_id(res_id).state == "unknown"
        events = repo.get_events_by_request_id(req_id)
        assert len(events) == 1
        assert events[0].status == "unknown"

        # Counters preserved across all four scopes
        for scope, scope_key, window_kind, window_start in scopes:
            ctr = repo.get_counter(
                scope=scope,
                scope_key=scope_key,
                window_kind=window_kind,
                window_start=window_start,
            )
            assert ctr is not None
            assert ctr.calls_reserved == 1
            assert ctr.calls_settled == 0
            assert ctr.cost_reserved_micros == cost_reserved
            assert ctr.cost_micros == 0


def test_reconciliation_repeat_idempotency(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """S1: Running reconciliation repeatedly produces zero duplicate operations."""
    settings = _make_test_settings(llm_reconciliation_stale_seconds=300)
    reconciler = LlmReconciliationService(
        session_factory=pg_session_factory, settings=settings
    )

    now = datetime.now(UTC)
    stale_time = now - timedelta(minutes=10)

    with pg_session_factory() as session:
        user_model, _ = _create_user(session)
        repo = LlmUsageRepository(session)
        repo.create_reservation(
            user_id=user_model.id,
            request_id=uuid4(),
            call_kind="guard",
            provider="openrouter",
            cost_reserved_micros=15000,
            created_at=stale_time,
        )
        session.commit()

    run1 = reconciler.reconcile_stale_reservations(now=now)
    assert run1.reconciled_reserved == 1

    run2 = reconciler.reconcile_stale_reservations(now=now)
    assert run2.reconciled_reserved == 0
    assert run2.reconciled_started == 0
    assert run2.returned_cost_micros == 0
