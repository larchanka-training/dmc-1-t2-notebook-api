"""Tests for LlmUsageRepository data-access layer and quota invariants."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.modules.auth.models.user import User as UserModel
from app.modules.llm.repositories import LlmUsageRepository


def _create_test_user(db: Session) -> UserModel:
    user = UserModel(
        id=uuid4(),
        email=f"test-{uuid4()}@notebook.local",
        display_name="Test User",
        created_at=datetime.now(UTC),
    )
    db.add(user)
    db.commit()
    return user


def test_entitlement_upsert_and_get(db_session: Session) -> None:
    """Verify entitlement creation, fetching, and updating for a user."""
    repo = LlmUsageRepository(db_session)
    user = _create_test_user(db_session)

    assert repo.get_entitlement(user.id) is None

    # Create new entitlement
    created = repo.upsert_entitlement(
        user_id=user.id,
        tier="free",
        daily_call_limit=20,
        monthly_call_limit=200,
    )
    assert created.user_id == user.id
    assert created.tier == "free"
    assert created.daily_call_limit == 20
    assert created.monthly_call_limit == 200

    fetched = repo.get_entitlement(user.id)
    assert fetched is not None
    assert fetched.tier == "free"

    # Update entitlement to developer tier
    updated = repo.upsert_entitlement(
        user_id=user.id,
        tier="developer",
        daily_call_limit=100,
        monthly_call_limit=1000,
    )
    assert updated.tier == "developer"
    assert updated.daily_call_limit == 100

    refetched = repo.get_entitlement(user.id)
    assert refetched is not None
    assert refetched.tier == "developer"


def test_reservation_lifecycle(db_session: Session) -> None:
    """Verify in-flight reservation creation and state transitions."""
    repo = LlmUsageRepository(db_session)
    user = _create_test_user(db_session)
    request_id = uuid4()

    reservation = repo.create_reservation(
        user_id=user.id,
        request_id=request_id,
        call_kind="guard",
        provider="openrouter",
        cost_reserved_micros=5000,
    )
    assert reservation.state == "reserved"
    assert reservation.started_at is None
    assert reservation.closed_at is None

    # Transition reserved -> started
    now = datetime.now(UTC)
    assert repo.transition_reservation_to_started(reservation.id, started_at=now) is True
    assert repo.transition_reservation_to_started(reservation.id) is False  # idempotent

    db_session.refresh(reservation)
    assert reservation.state == "started"
    assert reservation.started_at is not None

    # Transition started -> settled
    closed_now = now + timedelta(seconds=1)
    assert repo.transition_reservation_to_settled(reservation.id, closed_at=closed_now) is True
    assert repo.transition_reservation_to_settled(reservation.id) is False  # idempotent

    db_session.refresh(reservation)
    assert reservation.state == "settled"
    assert reservation.closed_at is not None

    # Query by request_id
    reservations = repo.get_reservations_by_request_id(request_id)
    assert len(reservations) == 1
    assert reservations[0].id == reservation.id


def test_reservation_atomic_release_claim(db_session: Session) -> None:
    """Verify atomic release claim returns bound once and is idempotent."""
    repo = LlmUsageRepository(db_session)
    user = _create_test_user(db_session)
    request_id = uuid4()

    reservation = repo.create_reservation(
        user_id=user.id,
        request_id=request_id,
        call_kind="generator",
        provider="bedrock",
        cost_reserved_micros=12_500,
    )

    # First release attempt succeeds and returns cost_reserved_micros
    bound = repo.transition_reservation_to_released(reservation.id)
    assert bound == 12_500

    db_session.refresh(reservation)
    assert reservation.state == "released"
    assert reservation.closed_at is not None

    # Concurrent or retried release attempt must return None (cannot double-claim)
    second_bound = repo.transition_reservation_to_released(reservation.id)
    assert second_bound is None

    # A reservation in started state cannot be released
    started_res = repo.create_reservation(
        user_id=user.id,
        request_id=request_id,
        call_kind="repair",
        provider="bedrock",
        cost_reserved_micros=8_000,
    )
    repo.transition_reservation_to_started(started_res.id)
    assert repo.transition_reservation_to_released(started_res.id) is None


def test_reservation_transition_to_unknown(db_session: Session) -> None:
    """Verify crash recovery transitions started reservation to unknown."""
    repo = LlmUsageRepository(db_session)
    user = _create_test_user(db_session)

    res = repo.create_reservation(
        user_id=user.id,
        request_id=uuid4(),
        call_kind="generator",
        provider="openrouter",
        cost_reserved_micros=10_000,
    )
    repo.transition_reservation_to_started(res.id)

    assert repo.transition_reservation_to_unknown(res.id) is True
    db_session.refresh(res)
    assert res.state == "unknown"
    assert res.closed_at is not None


def test_ledger_events(db_session: Session) -> None:
    """Verify recording and querying append-only ledger events."""
    repo = LlmUsageRepository(db_session)
    user = _create_test_user(db_session)
    request_id = uuid4()
    res1_id = uuid4()
    res2_id = uuid4()

    ev1 = repo.record_event(
        user_id=user.id,
        request_id=request_id,
        reservation_id=res1_id,
        call_kind="guard",
        provider="openrouter",
        model_id="openrouter/free",
        status="ok",
        prompt_tokens=150,
        completion_tokens=20,
        estimated_cost_micros=850,
    )
    ev2 = repo.record_event(
        user_id=user.id,
        request_id=request_id,
        reservation_id=res2_id,
        call_kind="generator",
        provider="openrouter",
        model_id="openrouter/free",
        status="ok",
        prompt_tokens=800,
        completion_tokens=350,
        estimated_cost_micros=4_500,
    )

    by_req = repo.get_events_by_request_id(request_id)
    assert len(by_req) == 2
    assert [e.id for e in by_req] == [ev1.id, ev2.id]

    by_user = repo.get_events_by_user_id(user.id)
    assert len(by_user) == 2


def test_reserve_quota_empty_table_refusal_when_exceeding_limit(
    db_session: Session,
) -> None:
    """The conditional INSERT on an empty table must reject if cost > limit."""
    repo = LlmUsageRepository(db_session)
    window_start = date(2026, 9, 17)

    # limit = 1, cost = 2 -> must reject immediately on empty table
    result = repo.reserve_quota(
        scope="user",
        scope_key="user-1",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=1000,
        call_limit=1,
        cost_limit_micros=None,
    )
    assert result is None

    # Verify no row was inserted
    counter = repo.get_counter(
        scope="user",
        scope_key="user-1",
        window_kind="day",
        window_start=window_start,
    )
    assert counter is None


def test_reserve_quota_empty_table_admission_and_initialization(
    db_session: Session,
) -> None:
    """First request initializes counters to zero and admits valid reservation."""
    repo = LlmUsageRepository(db_session)
    window_start = date(2026, 9, 17)

    # limit = 10, cost = 2 -> admits
    res = repo.reserve_quota(
        scope="user",
        scope_key="user-1",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=5000,
        call_limit=10,
        cost_limit_micros=50_000,
    )
    assert res == 2

    counter = repo.get_counter(
        scope="user",
        scope_key="user-1",
        window_kind="day",
        window_start=window_start,
    )
    assert counter is not None
    assert counter.calls_reserved == 2
    assert counter.calls_settled == 0
    assert counter.cost_reserved_micros == 5000
    assert counter.cost_micros == 0


def test_reserve_quota_existing_row_limit_enforcement(db_session: Session) -> None:
    """Existing counter row enforces limit and rejects overflow."""
    repo = LlmUsageRepository(db_session)
    window_start = date(2026, 9, 17)

    # First request: cost = 2, limit = 3 -> admitted (total 2)
    res1 = repo.reserve_quota(
        scope="user",
        scope_key="user-1",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=2000,
        call_limit=3,
        cost_limit_micros=None,
    )
    assert res1 == 2

    # Second request: cost = 2, limit = 3 -> 2 + 2 = 4 > 3 -> rejected
    res2 = repo.reserve_quota(
        scope="user",
        scope_key="user-1",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=2000,
        call_limit=3,
        cost_limit_micros=None,
    )
    assert res2 is None

    # Counter remains at 2
    counter = repo.get_counter(
        scope="user",
        scope_key="user-1",
        window_kind="day",
        window_start=window_start,
    )
    assert counter is not None
    assert counter.calls_reserved == 2


def test_reserve_quota_inactive_limit_dimensions_null(db_session: Session) -> None:
    """Unconstrained dimensions (NULL limit) admit valid requests unconditionally."""
    repo = LlmUsageRepository(db_session)
    window_start = date(2026, 9, 17)

    # user/day: call_limit = 10, cost_limit_micros = None
    r1 = repo.reserve_quota(
        scope="user",
        scope_key="user-null",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=100_000,
        call_limit=10,
        cost_limit_micros=None,
    )
    assert r1 == 2

    # global/month: call_limit = None, cost_limit_micros = 500_000
    r2 = repo.reserve_quota(
        scope="global",
        scope_key="-",
        window_kind="month",
        window_start=window_start,
        call_cost=50,
        cost_reserved_micros=200_000,
        call_limit=None,
        cost_limit_micros=500_000,
    )
    assert r2 == 50


def test_settle_quota(db_session: Session) -> None:
    """Settling a call moves cost from reserved to settled and increments calls_settled."""
    repo = LlmUsageRepository(db_session)
    window_start = date(2026, 9, 17)

    repo.reserve_quota(
        scope="user",
        scope_key="user-settle",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=10_000,
        call_limit=10,
        cost_limit_micros=None,
    )

    # Settle one of the calls: reserved was 5000, settled estimate is 3200
    repo.settle_quota(
        scope="user",
        scope_key="user-settle",
        window_kind="day",
        window_start=window_start,
        cost_reserved_micros=5_000,
        settled_cost_micros=3_200,
    )

    counter = repo.get_counter(
        scope="user",
        scope_key="user-settle",
        window_kind="day",
        window_start=window_start,
    )
    assert counter is not None
    assert counter.calls_reserved == 2
    assert counter.calls_settled == 1
    assert counter.cost_reserved_micros == 5_000  # 10_000 - 5_000
    assert counter.cost_micros == 3_200


def test_release_quota(db_session: Session) -> None:
    """Releasing a call decrements calls_reserved and cost_reserved_micros."""
    repo = LlmUsageRepository(db_session)
    window_start = date(2026, 9, 17)

    repo.reserve_quota(
        scope="user",
        scope_key="user-release",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=10_000,
        call_limit=10,
        cost_limit_micros=None,
    )

    # Release downstream call (e.g. guard rejected, generator call released)
    repo.release_quota(
        scope="user",
        scope_key="user-release",
        window_kind="day",
        window_start=window_start,
        cost_reserved_micros=5_000,
        call_count=1,
    )

    counter = repo.get_counter(
        scope="user",
        scope_key="user-release",
        window_kind="day",
        window_start=window_start,
    )
    assert counter is not None
    assert counter.calls_reserved == 1  # 2 - 1
    assert counter.cost_reserved_micros == 5_000  # 10_000 - 5_000
    assert counter.calls_settled == 0
    assert counter.cost_micros == 0


def test_reserve_quota_refreshes_retained_counter_identity(
    db_session: Session,
) -> None:
    """Retained counter ORM object must reflect subsequent reserve mutations before and after commit.

    Regression test for F1: when the session factory uses expire_on_commit=False,
    raw textual UPSERT statements must refresh any counter instance already loaded
    in the identity map.
    """
    repo = LlmUsageRepository(db_session)
    window_start = date(2026, 9, 17)

    # First reservation: 2 calls, 100 micros
    admitted1 = repo.reserve_quota(
        scope="user",
        scope_key="user-f1-regress",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=100,
        call_limit=10,
        cost_limit_micros=500,
    )
    assert admitted1 == 2

    # Retain the ORM object in memory
    retained = repo.get_counter(
        scope="user",
        scope_key="user-f1-regress",
        window_kind="day",
        window_start=window_start,
    )
    assert retained is not None
    assert retained.calls_reserved == 2
    assert retained.cost_reserved_micros == 100

    # Second reservation in the same session: another 2 calls, 100 micros
    admitted2 = repo.reserve_quota(
        scope="user",
        scope_key="user-f1-regress",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=100,
        call_limit=10,
        cost_limit_micros=500,
    )
    assert admitted2 == 4

    # Both call and cost counters on the retained object MUST update immediately
    assert retained.calls_reserved == 4
    assert retained.cost_reserved_micros == 200

    # Commit must not reset to stale values under expire_on_commit=False
    db_session.commit()
    assert retained.calls_reserved == 4
    assert retained.cost_reserved_micros == 200

    # get_counter in same session returns same updated instance
    refetched = repo.get_counter(
        scope="user",
        scope_key="user-f1-regress",
        window_kind="day",
        window_start=window_start,
    )
    assert refetched is retained
    assert refetched.calls_reserved == 4
    assert refetched.cost_reserved_micros == 200


def test_settle_and_release_quota_refresh_retained_counter_identity(
    db_session: Session,
) -> None:
    """Retained counter ORM object must reflect settle and release mutations before and after commit."""
    repo = LlmUsageRepository(db_session)
    window_start = date(2026, 9, 17)

    repo.reserve_quota(
        scope="user",
        scope_key="user-settle-release-retained",
        window_kind="day",
        window_start=window_start,
        call_cost=2,
        cost_reserved_micros=10_000,
        call_limit=10,
        cost_limit_micros=None,
    )

    retained = repo.get_counter(
        scope="user",
        scope_key="user-settle-release-retained",
        window_kind="day",
        window_start=window_start,
    )
    assert retained is not None
    assert retained.calls_reserved == 2
    assert retained.cost_reserved_micros == 10_000
    assert retained.calls_settled == 0
    assert retained.cost_micros == 0

    # Settle one call
    repo.settle_quota(
        scope="user",
        scope_key="user-settle-release-retained",
        window_kind="day",
        window_start=window_start,
        cost_reserved_micros=5_000,
        settled_cost_micros=3_200,
    )

    assert retained.calls_reserved == 2
    assert retained.calls_settled == 1
    assert retained.cost_reserved_micros == 5_000
    assert retained.cost_micros == 3_200

    db_session.commit()
    assert retained.calls_settled == 1
    assert retained.cost_reserved_micros == 5_000
    assert retained.cost_micros == 3_200

    # Release second call
    repo.release_quota(
        scope="user",
        scope_key="user-settle-release-retained",
        window_kind="day",
        window_start=window_start,
        cost_reserved_micros=5_000,
        call_count=1,
    )

    assert retained.calls_reserved == 1
    assert retained.cost_reserved_micros == 0
    assert retained.calls_settled == 1
    assert retained.cost_micros == 3_200

    db_session.commit()
    assert retained.calls_reserved == 1
    assert retained.cost_reserved_micros == 0


def test_reservation_state_transitions_refresh_retained_instance(
    db_session: Session,
) -> None:
    """Retained reservation instances must reflect state transitions before and after commit."""
    repo = LlmUsageRepository(db_session)
    user = _create_test_user(db_session)

    res = repo.create_reservation(
        user_id=user.id,
        request_id=uuid4(),
        call_kind="generator",
        provider="openai",
        cost_reserved_micros=5_000,
    )
    assert res.state == "reserved"
    assert res.started_at is None
    assert res.closed_at is None

    # Transition to started
    assert repo.transition_reservation_to_started(res.id) is True
    assert res.state == "started"
    assert res.started_at is not None

    db_session.commit()
    assert res.state == "started"

    # Transition to settled
    assert repo.transition_reservation_to_settled(res.id) is True
    assert res.state == "settled"
    assert res.closed_at is not None

    db_session.commit()
    assert res.state == "settled"

    # Create another reservation to test release transition
    res2 = repo.create_reservation(
        user_id=user.id,
        request_id=uuid4(),
        call_kind="guard",
        provider="bedrock",
        cost_reserved_micros=2_500,
    )
    assert res2.state == "reserved"

    claimed = repo.transition_reservation_to_released(res2.id)
    assert claimed == 2_500
    assert res2.state == "released"
    assert res2.closed_at is not None

    db_session.commit()
    assert res2.state == "released"
