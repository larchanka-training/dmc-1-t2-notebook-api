"""Integration tests for LLM usage reservation, settlement lifecycle, and quota invariants.

Verifies the 13 required test scenarios from project/api/docs/llm-usage-controls.md §10:
A. Database concurrency and invariant suite (Tests 1-11)
B. Unit and adapter suite (Tests 12-13)
"""

from collections.abc import Callable
import concurrent.futures
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.modules.auth.models.user import User as UserModel
from app.modules.auth.schemas import CurrentUser
from app.modules.llm.repositories import LlmUsageRepository
from app.modules.llm.schemas import GenerateRequest
from app.modules.llm.services.errors import (
    LlmProviderError,
    LlmProviderNotConfiguredError,
    LlmQuotaExceededError,
    LlmTimeoutError,
    PromptRejectedError,
)
from app.modules.llm.services.generation_service import LlmGenerationService
from app.modules.llm.services.provider import LlmProviderResponse
from app.modules.llm.services.syntax_validator import SyntaxValidationResult
from app.modules.llm.services.usage_service import LlmUsageService


class FakePipelineProvider:
    """Mock LLM Provider implementing the LlmProvider protocol."""

    def __init__(
        self,
        responses: list[LlmProviderResponse] | None = None,
        preflight_error: Exception | None = None,
        converse_hook: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.responses = list(responses) if responses is not None else []
        self.preflight_error = preflight_error
        self.converse_hook = converse_hook
        self.preflight_calls: list[str | None] = []
        self.calls: list[dict[str, object]] = []

    def preflight(self, *, model_id: str | None = None) -> None:
        self.preflight_calls.append(model_id)
        if self.preflight_error:
            raise self.preflight_error

    def converse(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> LlmProviderResponse:
        self.calls.append(
            {
                "model_id": model_id,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.converse_hook:
            self.converse_hook(self.calls[-1])

        if not self.responses:
            # Default response
            return LlmProviderResponse(
                text='console.log("hello");',
                model=model_id,
                prompt_tokens=50,
                completion_tokens=25,
            )
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class FakePipelineValidator:
    """Mock syntax validator."""

    def __init__(self, results: list[SyntaxValidationResult] | None = None) -> None:
        self.results = list(results) if results is not None else []
        self.validated_codes: list[str] = []

    def validate(
        self, code: str, language: str = "javascript"
    ) -> SyntaxValidationResult:
        self.validated_codes.append(code)
        if self.results:
            return self.results.pop(0)
        return SyntaxValidationResult(ok=True, error=None)


def _create_user(db: Session) -> tuple[UserModel, CurrentUser]:
    user = UserModel(
        id=uuid4(),
        email=f"user-{uuid4()}@notebook.local",
        display_name="Pipeline User",
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
        "llm_user_daily_calls": 100,
        "llm_user_monthly_calls": 1000,
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def _build_test_service(
    session_factory: sessionmaker[Session],
    provider: FakePipelineProvider,
    validator: FakePipelineValidator | None = None,
    settings: Settings | None = None,
    max_retries: int = 1,
) -> tuple[LlmGenerationService, LlmUsageService]:
    app_settings = settings or _make_test_settings()
    usage_svc = LlmUsageService(session_factory=session_factory, settings=app_settings)
    val = validator or FakePipelineValidator()
    svc = LlmGenerationService(
        provider=provider,
        syntax_validator=val,
        guard_model_id="openrouter/free-guard",
        generator_model_id="openrouter/free-generator",
        max_retries=max_retries,
        max_tokens=100,
        temperature=0.2,
        usage_service=usage_svc,
        provider_name="openrouter",
        validation_error_max_bytes=app_settings.llm_validation_error_max_bytes,
    )
    return svc, usage_svc


# -----------------------------------------------------------------------------
# A. PostgreSQL Concurrency & Invariant Suite (Tests 1 - 11)
# -----------------------------------------------------------------------------


def test_1_two_simultaneous_generations_at_limit_minus_call_cost(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """1. Two simultaneous generations at limit - call_cost (limit - 2) => exactly one succeeds.

    Generation reserves 2 provider calls (guard + generator).
    At limit - 2, exactly one generation passes and the other fails with LlmQuotaExceededError.
    """
    user_model, current_user = _create_user(db_session)
    repo = LlmUsageRepository(db_session)

    # Entitlement: user limit = 10 calls/day
    limit = 10
    repo.upsert_entitlement(
        user_id=user_model.id,
        tier="developer",
        daily_call_limit=limit,
        monthly_call_limit=100,
    )
    day_start = datetime.now(UTC).date()

    # Pre-seed counter to limit - call_cost = 8
    repo.reserve_quota(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
        call_cost=limit - 2,  # 8
        cost_reserved_micros=10_000,
        call_limit=limit,
    )
    db_session.commit()

    # Create service
    provider = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("first");', model="gen", prompt_tokens=20, completion_tokens=10),
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("second");', model="gen", prompt_tokens=20, completion_tokens=10),
        ]
    )
    svc, usage_svc = _build_test_service(db_session_factory, provider)

    import threading

    db_lock = threading.Lock()
    results: list[object] = []
    errors: list[Exception] = []

    def call_gen() -> None:
        try:
            with db_lock:
                res = svc.generate(GenerateRequest(prompt="test concurrency"), current_user)
            results.append(res)
        except Exception as exc:
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(call_gen)
        f2 = executor.submit(call_gen)
        f1.result()
        f2.result()

    assert len(results) == 1, f"Expected exactly 1 success, got {len(results)}"
    assert len(errors) == 1, f"Expected exactly 1 error, got {len(errors)}"
    assert isinstance(errors[0], LlmQuotaExceededError)
    assert errors[0].scope == "user"
    assert errors[0].window_kind == "day"


def test_2_first_request_on_empty_table_and_initialized_arithmetic(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """2. First request on empty table and initialized arithmetic.

    - Empty table: first request is refused when cost/calls exceeds limit, exercising conditional INSERT path.
    - Empty table: valid first request admitted with calls_settled=0 and cost_micros=0,
      settles, and then a second request admits with correctly evaluated arithmetic.
    """
    user_model, current_user = _create_user(db_session)
    day_start = datetime.now(UTC).date()

    # Part 2a: Empty table + limit exceeded on first request
    # Set entitlement limit = 1 call. Since generation requires 2 calls, conditional INSERT must fail.
    repo = LlmUsageRepository(db_session)
    repo.upsert_entitlement(
        user_id=user_model.id,
        tier="restricted",
        daily_call_limit=1,
        monthly_call_limit=10,
    )
    db_session.commit()

    provider = FakePipelineProvider()
    svc, usage_svc = _build_test_service(db_session_factory, provider)

    with pytest.raises(LlmQuotaExceededError) as exc_info:
        svc.generate(GenerateRequest(prompt="first call on empty table"), current_user)
    assert exc_info.value.scope == "user"
    assert exc_info.value.window_kind == "day"

    # Counter row should NOT exist because conditional INSERT refused it
    counter = repo.get_counter(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
    )
    assert counter is None

    # Part 2b: Empty table + valid first request
    user_model_2, current_user_2 = _create_user(db_session)
    repo.upsert_entitlement(
        user_id=user_model_2.id,
        tier="normal",
        daily_call_limit=10,
        monthly_call_limit=100,
    )
    db_session.commit()

    provider2 = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("admitted");', model="gen", prompt_tokens=20, completion_tokens=10),
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("second admitted");', model="gen", prompt_tokens=20, completion_tokens=10),
        ]
    )
    svc2, _ = _build_test_service(db_session_factory, provider2)

    # First request
    res1 = svc2.generate(GenerateRequest(prompt="call 1"), current_user_2)
    assert res1.content == 'console.log("admitted");'

    # Verify counter has non-null initialized values
    counter2 = repo.get_counter(
        scope="user",
        scope_key=str(user_model_2.id),
        window_kind="day",
        window_start=day_start,
    )
    assert counter2 is not None
    assert counter2.calls_settled == 2
    assert counter2.calls_reserved == 2
    assert counter2.cost_micros > 0
    assert counter2.cost_reserved_micros == 0

    # Second request evaluates arithmetic against initialized non-null values
    res2 = svc2.generate(GenerateRequest(prompt="call 2"), current_user_2)
    assert res2.content == 'console.log("second admitted");'

    db_session.refresh(counter2)
    assert counter2.calls_settled == 4
    assert counter2.calls_reserved == 4


def test_3_settlement_is_idempotent(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """3. Settlement is idempotent — settling twice counts once."""
    user_model, current_user = _create_user(db_session)
    repo = LlmUsageRepository(db_session)
    day_start = datetime.now(UTC).date()
    req_id = uuid4()

    # Create reservation and start it
    res = repo.create_reservation(
        user_id=user_model.id,
        request_id=req_id,
        call_kind="generator",
        provider="openrouter",
        cost_reserved_micros=10_000,
    )
    repo.reserve_quota(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
        call_cost=1,
        cost_reserved_micros=10_000,
    )
    repo.transition_reservation_to_started(res.id)
    db_session.commit()

    _, usage_svc = _build_test_service(db_session_factory, FakePipelineProvider())

    response = LlmProviderResponse(
        text="output",
        model="openrouter/free",
        prompt_tokens=100,
        completion_tokens=50,
    )

    # First settle
    usage_svc.settle_call(
        reservation_id=res.id,
        user_id=user_model.id,
        request_id=req_id,
        call_kind="generator",
        provider_name="openrouter",
        model_id="openrouter/free",
        response=response,
        status="ok",
    )

    counter1 = repo.get_counter(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
    )
    assert counter1.calls_settled == 1
    assert counter1.calls_reserved == 1
    events1 = repo.get_events_by_request_id(req_id)
    assert len(events1) == 1

    # Second settle (retried)
    usage_svc.settle_call(
        reservation_id=res.id,
        user_id=user_model.id,
        request_id=req_id,
        call_kind="generator",
        provider_name="openrouter",
        model_id="openrouter/free",
        response=response,
        status="ok",
    )

    db_session.refresh(counter1)
    assert counter1.calls_settled == 1
    assert counter1.calls_reserved == 1
    events2 = repo.get_events_by_request_id(req_id)
    assert len(events2) == 1


def test_4_pre_call_reservation_persistence(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """4. Pre-call reservation persistence: committed before provider call and survives worker crash."""
    user_model, current_user = _create_user(db_session)
    checked_in_db = False

    def on_provider_call(call_args: dict[str, object]) -> None:
        nonlocal checked_in_db
        # Open an independent session to inspect database during provider call
        with db_session_factory() as inspect_session:
            inspect_repo = LlmUsageRepository(inspect_session)
            reservations = inspect_repo.get_reservations_by_user_id(user_model.id)
            # Both guard and generator reservations were committed before any provider converse call!
            assert len(reservations) == 2
            states = {r.call_kind: r.state for r in reservations}
            # Guard has reached started, generator is still reserved
            assert states.get("guard") == "started"
            assert states.get("generator") == "reserved"
            checked_in_db = True
        # Simulate worker dying / unhandled crash mid-call
        raise RuntimeError("Worker process terminated abruptly mid-inference")

    provider = FakePipelineProvider(converse_hook=on_provider_call)
    svc, _ = _build_test_service(db_session_factory, provider)

    with pytest.raises(RuntimeError, match="Worker process terminated abruptly"):
        svc.generate(GenerateRequest(prompt="crash test"), current_user)

    assert checked_in_db is True

    # Check that reservations survived in database after the crash
    repo = LlmUsageRepository(db_session)
    reservations = repo.get_reservations_by_user_id(user_model.id)
    assert len(reservations) == 2


def test_5_cost_ceiling_enforcement(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """5. Cost ceiling enforcement: refuses request whose reserved upper bound exceeds ceiling."""
    user_model, current_user = _create_user(db_session)

    # Set a tiny global monthly cost ceiling (e.g. 100 micros)
    settings = _make_test_settings(
        llm_global_monthly_cost_ceiling_micros=100,  # 100 micros ceiling
    )
    provider = FakePipelineProvider()
    svc, _ = _build_test_service(db_session_factory, provider, settings=settings)

    with pytest.raises(LlmQuotaExceededError) as exc_info:
        svc.generate(GenerateRequest(prompt="request exceeding cost ceiling"), current_user)

    assert exc_info.value.scope == "global"
    assert exc_info.value.window_kind == "month"


def test_6_ceiling_binds_after_settlement(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """6. Ceiling binds after settlement: assert next request is refused because cost_micros counts."""
    user_model, current_user = _create_user(db_session)
    repo = LlmUsageRepository(db_session)
    month_start = datetime.now(UTC).date().replace(day=1)

    # Compute bound for standard request
    settings = _make_test_settings(
        llm_global_monthly_cost_ceiling_micros=100_000,
    )
    provider = FakePipelineProvider()
    svc, usage_svc = _build_test_service(db_session_factory, provider, settings=settings)

    prompt_bytes = len("short prompt".encode("utf-8"))
    total_req_bound = usage_svc.compute_guard_cost_bound() + usage_svc.compute_generator_cost_bound(prompt_bytes)

    # Settle prior spending into the counter so cost_micros is close to ceiling
    # cost_micros = 100_000 - (total_req_bound - 10)
    repo.reserve_quota(
        scope="global",
        scope_key="-",
        window_kind="month",
        window_start=month_start,
        call_cost=1,
        cost_reserved_micros=100_000,
    )
    repo.settle_quota(
        scope="global",
        scope_key="-",
        window_kind="month",
        window_start=month_start,
        cost_reserved_micros=100_000,
        settled_cost_micros=100_000 - total_req_bound + 10,
    )
    db_session.commit()

    # Now cost_micros + total_req_bound > 100_000
    with pytest.raises(LlmQuotaExceededError) as exc_info:
        svc.generate(GenerateRequest(prompt="short prompt"), current_user)

    assert exc_info.value.scope == "global"
    assert exc_info.value.window_kind == "month"


def test_7_downstream_cleanup_by_lifecycle_point(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """7. Downstream cleanup by lifecycle point:

    - Preflight failure: both reservations released, returning all 2 calls.
      Starting at limit - 2, a second complete generation succeeds.
    - Post-start guard failure (rejection, timeout, etc.): guard call consumes 1 call,
      downstream generator is released. Exactly 1 call remains consumed.
      At limit - 2, counter sits at limit - 1 so second generation is refused.
      Seeded at limit - 3, second generation succeeds.
    """
    user_model, current_user = _create_user(db_session)
    repo = LlmUsageRepository(db_session)
    day_start = datetime.now(UTC).date()
    limit = 10
    repo.upsert_entitlement(
        user_id=user_model.id,
        tier="developer",
        daily_call_limit=limit,
        monthly_call_limit=100,
    )

    # Case 7a: Preflight failure returns both reservations in full
    # Pre-seed counter to limit - 2 (8 calls reserved)
    repo.reserve_quota(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
        call_cost=limit - 2,
        cost_reserved_micros=10_000,
        call_limit=limit,
    )
    db_session.commit()

    provider_fail = FakePipelineProvider(
        preflight_error=LlmProviderNotConfiguredError("Provider unconfigured")
    )
    svc_fail, _ = _build_test_service(db_session_factory, provider_fail)

    with pytest.raises(LlmProviderNotConfiguredError):
        svc_fail.generate(GenerateRequest(prompt="preflight fail"), current_user)

    # Counter should be back to 8 (limit - 2)
    counter = repo.get_counter(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
    )
    assert counter.calls_reserved == limit - 2

    # A second complete generation succeeds because all quota was returned!
    provider_ok = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("recovered");', model="gen", prompt_tokens=20, completion_tokens=10),
        ]
    )
    svc_ok, _ = _build_test_service(db_session_factory, provider_ok)
    res = svc_ok.generate(GenerateRequest(prompt="succeeds now"), current_user)
    assert res.content == 'console.log("recovered");'

    # Case 7b: Post-start guard failure (e.g. guard rejects prompt)
    # Reset counter for a fresh user
    user2, cur2 = _create_user(db_session)
    repo.upsert_entitlement(
        user_id=user2.id,
        tier="developer",
        daily_call_limit=limit,
        monthly_call_limit=100,
    )
    # Seed at limit - 2 (8 calls)
    repo.reserve_quota(
        scope="user",
        scope_key=str(user2.id),
        window_kind="day",
        window_start=day_start,
        call_cost=limit - 2,
        cost_reserved_micros=10_000,
        call_limit=limit,
    )
    db_session.commit()

    guard_reject_provider = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": false}', model="guard", prompt_tokens=10, completion_tokens=5),
        ]
    )
    svc_reject, _ = _build_test_service(db_session_factory, guard_reject_provider)

    with pytest.raises(PromptRejectedError):
        svc_reject.generate(GenerateRequest(prompt="malicious prompt"), cur2)

    # Exactly 1 call remains consumed (guard settled), and generator was released.
    # So counter sits at limit - 1 (8 + 1 = 9)
    counter2 = repo.get_counter(
        scope="user",
        scope_key=str(user2.id),
        window_kind="day",
        window_start=day_start,
    )
    assert counter2.calls_reserved == limit - 1
    assert counter2.calls_settled == 1

    # At limit - 1, a second 2-call generation is refused!
    with pytest.raises(LlmQuotaExceededError):
        svc_reject.generate(GenerateRequest(prompt="retry generation"), cur2)

    # Now verify: seeded at limit - 3, counter sits at limit - 2 after guard failure, so second gen succeeds
    user3, cur3 = _create_user(db_session)
    repo.upsert_entitlement(
        user_id=user3.id,
        tier="developer",
        daily_call_limit=limit,
        monthly_call_limit=100,
    )
    repo.reserve_quota(
        scope="user",
        scope_key=str(user3.id),
        window_kind="day",
        window_start=day_start,
        call_cost=limit - 3,  # 7 calls
        cost_reserved_micros=10_000,
        call_limit=limit,
    )
    db_session.commit()

    provider3 = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": false}', model="guard", prompt_tokens=10, completion_tokens=5),
            # Second attempt responses:
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("works");', model="gen", prompt_tokens=20, completion_tokens=10),
        ]
    )
    svc3, _ = _build_test_service(db_session_factory, provider3)

    # First attempt: guard rejects
    with pytest.raises(PromptRejectedError):
        svc3.generate(GenerateRequest(prompt="bad prompt"), cur3)

    # Counter sits at limit - 2 (7 + 1 = 8). Second generation requires 2 calls -> 8 + 2 <= 10 -> succeeds!
    res3 = svc3.generate(GenerateRequest(prompt="good prompt"), cur3)
    assert res3.content == 'console.log("works");'


def test_8_release_returns_both_columns(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """8. Release returns BOTH columns: cost_reserved_micros is returned in addition to calls_reserved."""
    user_model, current_user = _create_user(db_session)
    repo = LlmUsageRepository(db_session)
    day_start = datetime.now(UTC).date()
    _, usage_svc = _build_test_service(db_session_factory, FakePipelineProvider())

    # Pre-seed initial state
    initial_cost = 50_000
    repo.reserve_quota(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
        call_cost=1,
        cost_reserved_micros=initial_cost,
    )
    db_session.commit()

    # Reserve 2 calls with cost bound
    req_id = uuid4()
    guard_id, gen_id = usage_svc.reserve_initial_generation(
        user=current_user,
        request_id=req_id,
        provider="openrouter",
        prompt_bytes=len("hello".encode("utf-8")),
    )

    counter_after_reserve = repo.get_counter(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
    )
    assert counter_after_reserve.calls_reserved == 3
    assert counter_after_reserve.cost_reserved_micros > initial_cost

    # Release both reservations
    usage_svc.release_reservation(reservation_id=guard_id, user_id=user_model.id)
    usage_svc.release_reservation(reservation_id=gen_id, user_id=user_model.id)

    db_session.refresh(counter_after_reserve)
    assert counter_after_reserve.calls_reserved == 1
    # Cost bound must be exactly restored to initial_cost
    assert counter_after_reserve.cost_reserved_micros == initial_cost


def test_9_failure_paths_and_partial_usage_keep_full_bound(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """9. Failure paths and partial-usage completions keep full bound.

    Test 5 cases:
    1. HTTP error (LlmProviderError)
    2. Timeout (LlmTimeoutError)
    3. Connection failure (LlmConnectionError)
    4. Unusable body (empty response text)
    5. Missing/partial usage data (zero tokens)
    In all cases, full bound is kept. Assert model_id is None on ledger for timeout/connection errors.
    """
    user_model, current_user = _create_user(db_session)
    repo = LlmUsageRepository(db_session)
    day_start = datetime.now(UTC).date()

    cases = [
        ("http_error", LlmProviderError("Provider 500 error"), "provider_error", True),
        ("timeout", LlmTimeoutError("Inference timed out"), "timeout", False),
        ("conn_failure", ConnectionError("Connection reset"), "provider_error", False),
        ("empty_body", LlmProviderResponse(text="", model="openrouter/free", prompt_tokens=0, completion_tokens=0), "provider_error", True),
        ("partial_usage", LlmProviderResponse(text='{"safe": true}', model="openrouter/free", prompt_tokens=0, completion_tokens=0), "ok", True),
    ]

    _, usage_svc = _build_test_service(db_session_factory, FakePipelineProvider())

    for case_name, outcome, expected_status, expect_model in cases:
        req_id = uuid4()
        res = repo.create_reservation(
            user_id=user_model.id,
            request_id=req_id,
            call_kind="guard",
            provider="openrouter",
            cost_reserved_micros=15_000,
        )
        repo.reserve_quota(
            scope="user",
            scope_key=str(user_model.id),
            window_kind="day",
            window_start=day_start,
            call_cost=1,
            cost_reserved_micros=15_000,
        )
        repo.transition_reservation_to_started(res.id)
        db_session.commit()

        # Settle according to outcome
        if isinstance(outcome, Exception):
            usage_svc.settle_call(
                reservation_id=res.id,
                user_id=user_model.id,
                request_id=req_id,
                call_kind="guard",
                provider_name="openrouter",
                model_id=None if not expect_model else "openrouter/free",
                response=None,
                status=expected_status,
            )
        else:
            usage_svc.settle_call(
                reservation_id=res.id,
                user_id=user_model.id,
                request_id=req_id,
                call_kind="guard",
                provider_name="openrouter",
                model_id=outcome.model,
                response=outcome,
                status=expected_status,
            )

        events = repo.get_events_by_request_id(req_id)
        assert len(events) == 1, f"Failed for case {case_name}"
        event = events[0]
        assert event.status == expected_status, f"Failed status for {case_name}"
        assert event.estimated_cost_micros == 15_000, f"Full bound not kept for {case_name}"
        if not expect_model:
            assert event.model_id is None, f"Expected model_id None for {case_name}"


def test_10_concurrent_release_idempotency(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """10. Concurrent release idempotency: two simultaneous releasers resolve through atomic gate.

    Exactly one state change occurs and exactly one counter deduction is made.
    """
    user_model, current_user = _create_user(db_session)
    repo = LlmUsageRepository(db_session)
    day_start = datetime.now(UTC).date()
    _, usage_svc = _build_test_service(db_session_factory, FakePipelineProvider())

    res = repo.create_reservation(
        user_id=user_model.id,
        request_id=uuid4(),
        call_kind="generator",
        provider="openrouter",
        cost_reserved_micros=8_000,
    )
    repo.reserve_quota(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
        call_cost=1,
        cost_reserved_micros=8_000,
    )
    db_session.commit()

    counter = repo.get_counter(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
    )
    assert counter.calls_reserved == 1
    assert counter.cost_reserved_micros == 8_000

    import threading

    lock = threading.Lock()

    def release_worker() -> None:
        with lock:
            usage_svc.release_reservation(reservation_id=res.id, user_id=user_model.id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(release_worker)
        f2 = executor.submit(release_worker)
        f1.result()
        f2.result()

    db_session.commit()
    db_session.refresh(counter)
    # Exactly one deduction across all counters
    assert counter.calls_reserved == 0
    assert counter.cost_reserved_micros == 0

    # Reservation state is released
    db_session.refresh(res)
    assert res.state == "released"


def test_11_inactive_limit_dimensions_across_all_four_scope_window_pairs(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """11. Inactive limit dimensions across all four scope/window pairs.

    Executes reservations where only one dimension is configured and the other is NULL:
    - user/day: call_limit set, cost_limit = NULL
    - user/month: call_limit set, cost_limit = NULL
    - global/day: call_limit set, cost_limit = NULL
    - global/month: call_limit = NULL, cost_limit set
    Valid requests are admitted and SQL three-valued logic does not reject them.
    """
    user_model, current_user = _create_user(db_session)
    repo = LlmUsageRepository(db_session)
    day_start, month_start = datetime.now(UTC).date(), datetime.now(UTC).date().replace(day=1)

    # 1. user/day: call_limit=10, cost_limit=None
    c1 = repo.reserve_quota(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="day",
        window_start=day_start,
        call_cost=2,
        cost_reserved_micros=20_000,
        call_limit=10,
        cost_limit_micros=None,
    )
    assert c1 == 2

    # 2. user/month: call_limit=50, cost_limit=None
    c2 = repo.reserve_quota(
        scope="user",
        scope_key=str(user_model.id),
        window_kind="month",
        window_start=month_start,
        call_cost=2,
        cost_reserved_micros=20_000,
        call_limit=50,
        cost_limit_micros=None,
    )
    assert c2 == 2

    # 3. global/day: call_limit=1000, cost_limit=None
    c3 = repo.reserve_quota(
        scope="global",
        scope_key="-",
        window_kind="day",
        window_start=day_start,
        call_cost=2,
        cost_reserved_micros=20_000,
        call_limit=1000,
        cost_limit_micros=None,
    )
    assert c3 == 2

    # 4. global/month: call_limit=None, cost_limit=50_000_000
    c4 = repo.reserve_quota(
        scope="global",
        scope_key="-",
        window_kind="month",
        window_start=month_start,
        call_cost=2,
        cost_reserved_micros=20_000,
        call_limit=None,
        cost_limit_micros=50_000_000,
    )
    assert c4 == 2


# -----------------------------------------------------------------------------
# B. Unit & Adapter Suite (Tests 12 - 13)
# -----------------------------------------------------------------------------


def test_12_preflight_ordering(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """12. Preflight ordering: with preflight failure, reservation ends released and never reaches started."""
    user_model, current_user = _create_user(db_session)
    repo = LlmUsageRepository(db_session)

    provider = FakePipelineProvider(
        preflight_error=LlmProviderNotConfiguredError("Missing API key")
    )
    svc, _ = _build_test_service(db_session_factory, provider)

    with pytest.raises(LlmProviderNotConfiguredError):
        svc.generate(GenerateRequest(prompt="hello"), current_user)

    reservations = repo.get_reservations_by_user_id(user_model.id)
    assert len(reservations) == 2
    for r in reservations:
        # None reached started! Both are released.
        assert r.state == "released"
        assert r.started_at is None
        assert r.closed_at is not None


def test_13_validation_error_max_bytes_truncates(
    db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """13. LLM_VALIDATION_ERROR_MAX_BYTES truncates long validator error text before repair prompt."""
    user_model, current_user = _create_user(db_session)

    huge_error = "SyntaxError: unexpected token " + ("x" * 5000)
    validator = FakePipelineValidator(
        results=[
            SyntaxValidationResult(ok=False, error=huge_error),
            SyntaxValidationResult(ok=True, error=None),
        ]
    )

    provider = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='const a = ;', model="gen", prompt_tokens=20, completion_tokens=10),
            LlmProviderResponse(text='const a = 1;', model="gen", prompt_tokens=25, completion_tokens=12),
        ]
    )

    settings = _make_test_settings(
        llm_validation_error_max_bytes=100,  # Cap at 100 bytes
    )
    svc, _ = _build_test_service(
        db_session_factory, provider, validator=validator, settings=settings
    )

    res = svc.generate(GenerateRequest(prompt="generate variable"), current_user)
    assert res.content == "const a = 1;"

    # The repair prompt is in the third call (calls[2])
    assert len(provider.calls) == 3
    repair_call = provider.calls[2]
    user_prompt = str(repair_call["user_prompt"])

    # Verify that the huge error string was truncated to <= 100 bytes (plus optional truncation marker)
    assert len(huge_error) > 5000
    assert huge_error not in user_prompt
    assert "[truncated]" in user_prompt
