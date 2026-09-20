"""API integration tests for LLM usage views and reconciliation endpoints (Step 8e-3).

Tests:
1. GET /api/v1/llm/usage:
   - 401 when unauthenticated
   - 200 with caller's quota counters, active tier limits, and reset times
2. GET /api/v1/llm/admin/usage:
   - 401 when unauthenticated
   - 403 when authenticated as a non-allowlisted user
   - 200 when allowlisted: returns globalDay, globalMonth, recentReservations, recentEvents
3. POST /api/v1/llm/admin/reconcile:
   - 401 when unauthenticated
   - 403 when authenticated as a non-allowlisted user
   - 200 when allowlisted: runs reconciliation and returns summary
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, settings as app_settings
from app.main import app
from app.modules.llm.dependencies import (
    get_llm_reconciliation_service,
    get_llm_usage_service,
)
from app.modules.llm.repositories import LlmUsageRepository
from app.modules.llm.services.reconciliation_service import LlmReconciliationService
from app.modules.llm.services.usage_service import LlmUsageService


def _login(
    client: TestClient, email: str = "llm-view-user@example.com"
) -> tuple[dict[str, str], UUID]:
    otp = client.post(
        f"{app_settings.api_prefix}/auth/otp/request",
        json={"email": email},
    ).json()["otp"]
    body = client.post(
        f"{app_settings.api_prefix}/auth/otp/verify",
        json={"email": email, "otp": otp},
    ).json()
    user_id = UUID(body["user"]["id"])
    return {"Authorization": f"Bearer {body['accessToken']}"}, user_id


def test_get_user_usage_unauthenticated(client: TestClient) -> None:
    """GET /api/v1/llm/usage requires authentication."""
    response = client.get(f"{app_settings.api_prefix}/llm/usage")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_get_user_usage_authenticated(
    client: TestClient,
    db_session_factory: sessionmaker[Session],
) -> None:
    """GET /api/v1/llm/usage returns caller's current usage, limits, and reset times."""
    headers, user_id = _login(client, email="user-usage@example.com")

    test_settings = Settings(
        _env_file=None,
        app_env="dev",
        jwt_secret="x" * 32,
        otp_hash_secret="y" * 32,
        llm_free_tier_daily_calls=25,
        llm_free_tier_monthly_calls=250,
    )
    usage_svc = LlmUsageService(
        session_factory=db_session_factory, settings=test_settings
    )
    app.dependency_overrides[get_llm_usage_service] = lambda: usage_svc

    try:
        # Pre-seed one counter row for this user
        now = datetime.now(UTC)
        day_start, month_start = usage_svc.get_window_dates(now)
        with db_session_factory() as session:
            repo = LlmUsageRepository(session)
            repo.reserve_quota(
                scope="user",
                scope_key=str(user_id),
                window_kind="day",
                window_start=day_start,
                call_cost=2,
                cost_reserved_micros=15000,
            )
            session.commit()

        response = client.get(f"{app_settings.api_prefix}/llm/usage", headers=headers)
        assert response.status_code == 200
        data = response.json()

        assert data["userId"] == str(user_id)
        assert data["tier"] == "free"

        # Day window
        day = data["day"]
        assert day["scope"] == "user"
        assert day["windowKind"] == "day"
        assert day["windowStart"] == day_start.isoformat()
        assert day["callsReserved"] == 2
        assert day["callsSettled"] == 0
        assert day["callsTotal"] == 2
        assert day["callLimit"] == 25
        assert day["costReservedMicros"] == 15000
        assert day["costMicros"] == 0
        assert day["costTotalMicros"] == 15000
        assert day["costLimitMicros"] is None
        assert "resetsAt" in day
        assert isinstance(day["retryAfter"], int)
        assert day["retryAfter"] > 0

        # Month window (was not pre-seeded, so default zeros)
        month = data["month"]
        assert month["scope"] == "user"
        assert month["windowKind"] == "month"
        assert month["windowStart"] == month_start.isoformat()
        assert month["callsReserved"] == 0
        assert month["callsSettled"] == 0
        assert month["callsTotal"] == 0
        assert month["callLimit"] == 250
        assert month["costReservedMicros"] == 0
        assert month["costMicros"] == 0
        assert month["costTotalMicros"] == 0
        assert month["costLimitMicros"] is None
        assert "resetsAt" in month
        assert isinstance(month["retryAfter"], int)
        assert month["retryAfter"] > 0
    finally:
        app.dependency_overrides.pop(get_llm_usage_service, None)


def test_get_admin_usage_unauthenticated(client: TestClient) -> None:
    """GET /api/v1/llm/admin/usage requires authentication."""
    response = client.get(f"{app_settings.api_prefix}/llm/admin/usage")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_get_admin_usage_forbidden_for_non_allowlisted(client: TestClient) -> None:
    """GET /api/v1/llm/admin/usage returns 403 for non-allowlisted users."""
    headers, _ = _login(client, email="regular-user@example.com")

    # Temporarily set allowlist
    original_admin = app_settings.llm_admin_emails
    original_allowed = app_settings.llm_allowed_emails
    app_settings.llm_admin_emails = ""
    app_settings.llm_allowed_emails = "dev@example.com"
    try:
        response = client.get(
            f"{app_settings.api_prefix}/llm/admin/usage", headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "llm_admin_access_denied"
    finally:
        app_settings.llm_admin_emails = original_admin
        app_settings.llm_allowed_emails = original_allowed


def test_admin_endpoints_fail_closed_when_allowlist_is_empty(
    client: TestClient,
) -> None:
    """F1 Regression: Admin routes return 403 when allowlists are empty (fail-closed)."""
    headers, _ = _login(client, email="any-authenticated-user@example.com")

    original_admin = app_settings.llm_admin_emails
    original_allowed = app_settings.llm_allowed_emails
    app_settings.llm_admin_emails = ""
    app_settings.llm_allowed_emails = ""
    try:
        # GET /admin/usage returns 403
        res_get = client.get(
            f"{app_settings.api_prefix}/llm/admin/usage", headers=headers
        )
        assert res_get.status_code == 403
        assert res_get.json()["error"]["code"] == "llm_admin_access_denied"

        # POST /admin/reconcile returns 403
        res_post = client.post(
            f"{app_settings.api_prefix}/llm/admin/reconcile", headers=headers
        )
        assert res_post.status_code == 403
        assert res_post.json()["error"]["code"] == "llm_admin_access_denied"

        # Even with whitespace-only allowlist
        app_settings.llm_admin_emails = "   "
        app_settings.llm_allowed_emails = " ,  "
        res_ws = client.get(
            f"{app_settings.api_prefix}/llm/admin/usage", headers=headers
        )
        assert res_ws.status_code == 403
        assert res_ws.json()["error"]["code"] == "llm_admin_access_denied"
    finally:
        app_settings.llm_admin_emails = original_admin
        app_settings.llm_allowed_emails = original_allowed


def test_admin_endpoints_accessible_via_dedicated_admin_emails(
    client: TestClient,
    db_session_factory: sessionmaker[Session],
) -> None:
    """F1: Admin routes return 200 when user is in LLM_ADMIN_EMAILS (even if LLM_ALLOWED_EMAILS is empty)."""
    admin_email = "superadmin@example.com"
    headers, _ = _login(client, email=admin_email)

    original_admin = app_settings.llm_admin_emails
    original_allowed = app_settings.llm_allowed_emails
    app_settings.llm_admin_emails = f"other@example.com, {admin_email}"
    app_settings.llm_allowed_emails = ""  # Public generation, but private admin!

    test_settings = Settings(
        _env_file=None,
        app_env="dev",
        jwt_secret="x" * 32,
        otp_hash_secret="y" * 32,
        llm_admin_emails=app_settings.llm_admin_emails,
        llm_allowed_emails="",
    )
    usage_svc = LlmUsageService(
        session_factory=db_session_factory, settings=test_settings
    )
    reconciler = LlmReconciliationService(
        session_factory=db_session_factory, settings=test_settings
    )
    app.dependency_overrides[get_llm_usage_service] = lambda: usage_svc
    app.dependency_overrides[get_llm_reconciliation_service] = lambda: reconciler

    try:
        res_get = client.get(
            f"{app_settings.api_prefix}/llm/admin/usage", headers=headers
        )
        assert res_get.status_code == 200

        headers_post, _ = _login(client, email=admin_email)
        res_post = client.post(
            f"{app_settings.api_prefix}/llm/admin/reconcile", headers=headers_post
        )
        assert res_post.status_code == 200
    finally:
        app_settings.llm_admin_emails = original_admin
        app_settings.llm_allowed_emails = original_allowed
        app.dependency_overrides.pop(get_llm_usage_service, None)
        app.dependency_overrides.pop(get_llm_reconciliation_service, None)


def test_get_admin_usage_success_for_allowlisted(
    client: TestClient,
    db_session_factory: sessionmaker[Session],
) -> None:
    """GET /api/v1/llm/admin/usage returns global usage and recent activity for allowlisted user."""
    dev_email = "dev-allowlisted@example.com"
    headers, user_id = _login(client, email=dev_email)

    original_allowed = app_settings.llm_allowed_emails
    app_settings.llm_allowed_emails = dev_email

    test_settings = Settings(
        _env_file=None,
        app_env="dev",
        jwt_secret="x" * 32,
        otp_hash_secret="y" * 32,
        llm_allowed_emails=dev_email,
        llm_global_daily_calls=5000,
        llm_global_monthly_cost_ceiling_micros=50_000_000,
    )
    usage_svc = LlmUsageService(
        session_factory=db_session_factory, settings=test_settings
    )
    app.dependency_overrides[get_llm_usage_service] = lambda: usage_svc

    try:
        now = datetime.now(UTC)
        day_start, month_start = usage_svc.get_window_dates(now)

        # Pre-seed global counters and recent items
        with db_session_factory() as session:
            repo = LlmUsageRepository(session)
            repo.reserve_quota(
                scope="global",
                scope_key="-",
                window_kind="day",
                window_start=day_start,
                call_cost=5,
                cost_reserved_micros=20000,
            )
            req_id = uuid4()
            res = repo.create_reservation(
                user_id=user_id,
                request_id=req_id,
                call_kind="generator",
                provider="openrouter",
                cost_reserved_micros=20000,
                created_at=now,
            )
            repo.record_event(
                user_id=user_id,
                request_id=req_id,
                reservation_id=res.id,
                call_kind="generator",
                provider="openrouter",
                model_id="openrouter/generator",
                status="ok",
                prompt_tokens=100,
                completion_tokens=50,
                estimated_cost_micros=18000,
                created_at=now,
            )
            session.commit()

        response = client.get(
            f"{app_settings.api_prefix}/llm/admin/usage", headers=headers
        )
        assert response.status_code == 200
        data = response.json()

        # Global day
        global_day = data["globalDay"]
        assert global_day["scope"] == "global"
        assert global_day["windowKind"] == "day"
        assert global_day["callsReserved"] == 5
        assert global_day["callLimit"] == 5000
        assert global_day["costReservedMicros"] == 20000

        # Global month
        global_month = data["globalMonth"]
        assert global_month["scope"] == "global"
        assert global_month["windowKind"] == "month"
        assert global_month["costLimitMicros"] == 50_000_000

        # Recent activity
        assert len(data["recentReservations"]) >= 1
        assert data["recentReservations"][0]["id"] == str(res.id)
        assert data["recentReservations"][0]["callKind"] == "generator"

        assert len(data["recentEvents"]) >= 1
        assert data["recentEvents"][0]["reservationId"] == str(res.id)
        assert data["recentEvents"][0]["status"] == "ok"
    finally:
        app_settings.llm_allowed_emails = original_allowed
        app.dependency_overrides.pop(get_llm_usage_service, None)


def test_post_admin_reconcile_api(
    client: TestClient,
    db_session_factory: sessionmaker[Session],
) -> None:
    """POST /api/v1/llm/admin/reconcile runs reconciliation for allowlisted admin."""
    dev_email = "admin-reconciler@example.com"
    headers, user_id = _login(client, email=dev_email)

    original_allowed = app_settings.llm_allowed_emails
    app_settings.llm_allowed_emails = dev_email

    test_settings = Settings(
        _env_file=None,
        app_env="dev",
        jwt_secret="x" * 32,
        otp_hash_secret="y" * 32,
        llm_allowed_emails=dev_email,
        llm_reconciliation_stale_seconds=300,
    )
    reconciler = LlmReconciliationService(
        session_factory=db_session_factory, settings=test_settings
    )
    app.dependency_overrides[get_llm_reconciliation_service] = lambda: reconciler

    try:
        # Pre-seed a stale reservation
        stale_time = datetime.now(UTC) - timedelta(minutes=15)
        with db_session_factory() as session:
            repo = LlmUsageRepository(session)
            repo.create_reservation(
                user_id=user_id,
                request_id=uuid4(),
                call_kind="guard",
                provider="openrouter",
                cost_reserved_micros=10000,
                created_at=stale_time,
            )
            session.commit()

        response = client.post(
            f"{app_settings.api_prefix}/llm/admin/reconcile", headers=headers
        )
        assert response.status_code == 200
        data = response.json()

        assert data["reconciledReserved"] == 1
        assert data["reconciledStarted"] == 0
        assert data["returnedCostMicros"] == 10000
        assert "cutoff" in data
    finally:
        app_settings.llm_allowed_emails = original_allowed
        app.dependency_overrides.pop(get_llm_reconciliation_service, None)


def test_calls_total_after_settled_calls(
    client: TestClient,
    db_session_factory: sessionmaker[Session],
) -> None:
    """F2 Regression: Verify callsTotal does NOT double-count settled calls (reports 2, not 4)."""
    admin_email = "f2-admin@example.com"
    headers, user_id = _login(client, email=admin_email)

    original_admin = app_settings.llm_admin_emails
    app_settings.llm_admin_emails = admin_email

    test_settings = Settings(
        _env_file=None,
        app_env="dev",
        jwt_secret="x" * 32,
        otp_hash_secret="y" * 32,
        llm_admin_emails=admin_email,
        llm_free_tier_daily_calls=50,
        llm_global_daily_calls=500,
    )
    usage_svc = LlmUsageService(
        session_factory=db_session_factory, settings=test_settings
    )
    app.dependency_overrides[get_llm_usage_service] = lambda: usage_svc

    try:
        now = datetime.now(UTC)
        day_start, month_start = usage_svc.get_window_dates(now)

        with db_session_factory() as session:
            repo = LlmUsageRepository(session)
            # Step 1: Reserve 2 calls (e.g. guard + generator)
            for scope, scope_key in [("user", str(user_id)), ("global", "-")]:
                repo.reserve_quota(
                    scope=scope,
                    scope_key=scope_key,
                    window_kind="day",
                    window_start=day_start,
                    call_cost=2,
                    cost_reserved_micros=10000,
                )
                repo.reserve_quota(
                    scope=scope,
                    scope_key=scope_key,
                    window_kind="month",
                    window_start=month_start,
                    call_cost=2,
                    cost_reserved_micros=10000,
                )
            # Step 2: Settle both calls (calls_settled becomes 2, cost moves to cost_micros)
            for scope, scope_key in [("user", str(user_id)), ("global", "-")]:
                for _ in range(2):
                    repo.settle_quota(
                        scope=scope,
                        scope_key=scope_key,
                        window_kind="day",
                        window_start=day_start,
                        cost_reserved_micros=5000,
                        settled_cost_micros=4250,
                    )
                    repo.settle_quota(
                        scope=scope,
                        scope_key=scope_key,
                        window_kind="month",
                        window_start=month_start,
                        cost_reserved_micros=5000,
                        settled_cost_micros=4250,
                    )
            session.commit()

        # Check User View: callsTotal must be 2, NOT 2 + 2 = 4
        res_user = client.get(f"{app_settings.api_prefix}/llm/usage", headers=headers)
        assert res_user.status_code == 200
        user_data = res_user.json()
        assert user_data["day"]["callsReserved"] == 2
        assert user_data["day"]["callsSettled"] == 2
        assert user_data["day"]["callsTotal"] == 2  # NOT 4!
        assert user_data["month"]["callsTotal"] == 2

        # Check Admin View: callsTotal must be 2, NOT 4
        res_admin = client.get(
            f"{app_settings.api_prefix}/llm/admin/usage", headers=headers
        )
        assert res_admin.status_code == 200
        admin_data = res_admin.json()
        assert admin_data["globalDay"]["callsReserved"] == 2
        assert admin_data["globalDay"]["callsSettled"] == 2
        assert admin_data["globalDay"]["callsTotal"] == 2  # NOT 4!
        assert admin_data["globalMonth"]["callsTotal"] == 2
    finally:
        app_settings.llm_admin_emails = original_admin
        app.dependency_overrides.pop(get_llm_usage_service, None)
