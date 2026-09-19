"""Integration tests for LLM usage reservation, settlement lifecycle, and quota invariants.

Verifies the 13 required test scenarios from project/api/docs/llm-usage-controls.md §10:
A. Database concurrency and invariant suite (Tests 1-11)
B. Unit and adapter suite (Tests 12-13)
"""

import concurrent.futures
import json
import multiprocessing
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.modules.auth.models.user import User as UserModel
from app.modules.auth.schemas import CurrentUser
from app.modules.llm.repositories import LlmUsageRepository
from app.modules.llm.schemas import GenerateRequest, LlmContextCell
from app.modules.llm.services.errors import (
    LlmProviderNotConfiguredError,
    LlmQuotaExceededError,
    LlmServiceError,
    PromptRejectedError,
)
from app.modules.llm.services.generation_service import (
    LlmGenerationService,
    _build_generation_prompt,
    _build_guard_prompt,
    _truncate_validation_error,
)
from app.modules.llm.services.openrouter_client import HttpResponse, OpenRouterClient
from app.modules.llm.services.provider import LlmProviderResponse
from app.modules.llm.services.syntax_validator import SyntaxValidationResult
from app.modules.llm.services.usage_service import LlmUsageService


def _worker_crash_mid_call(db_url: str, user_id: UUID) -> None:
    """Worker process that starts generation and terminates abruptly with os._exit(42)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.modules.auth.schemas import CurrentUser
    from app.modules.llm.schemas import GenerateRequest
    from app.modules.llm.services.provider import LlmProviderResponse

    engine = create_engine(db_url)
    factory = sessionmaker(bind=engine)
    user = CurrentUser(
        id=user_id,
        email="worker@notebook.local",
        display_name="Worker",
        roles=[],
    )

    def crash_hook(call_args: dict[str, object]) -> None:
        # Pre-call reservations are committed to DB before converse.
        # Now kill process ungracefully with os._exit (no cleanup or finally blocks).
        os._exit(42)

    provider = FakePipelineProvider(
        converse_hook=crash_hook,
        responses=[
            LlmProviderResponse(
                text='{"safe": true}',
                model="guard",
                prompt_tokens=10,
                completion_tokens=5,
            )
        ],
    )
    svc, _ = _build_test_service(factory, provider)
    svc.generate(GenerateRequest(prompt="crash test"), user)



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
        guard_output_tokens_max=app_settings.llm_guard_output_tokens_max,
    )
    return svc, usage_svc


# -----------------------------------------------------------------------------
# A. PostgreSQL Concurrency & Invariant Suite (Tests 1 - 11)
# -----------------------------------------------------------------------------


def test_1_two_simultaneous_generations_at_limit_minus_call_cost(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """1. Two simultaneous generations at limit - call_cost (limit - 2) => exactly one succeeds.

    Generation reserves 2 provider calls (guard + generator).
    At limit - 2, exactly one generation passes and the other fails with LlmQuotaExceededError.
    Verified on real PostgreSQL without Python locks using threading.Barrier(2).
    """
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        repo = LlmUsageRepository(session)

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
        session.commit()

    # Create service with PostgreSQL session factory
    provider = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("first");', model="gen", prompt_tokens=20, completion_tokens=10),
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("second");', model="gen", prompt_tokens=20, completion_tokens=10),
        ]
    )
    svc, _ = _build_test_service(pg_session_factory, provider)

    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[Exception] = []

    def call_gen() -> None:
        barrier.wait()
        try:
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
    pg_session_factory: sessionmaker[Session],
) -> None:
    """2. First request on empty table and initialized arithmetic.

    - Empty table: first request is refused when cost/calls exceeds limit, exercising conditional INSERT path.
    - Empty table: valid first request admitted with calls_settled=0 and cost_micros=0,
      settles, and then a second request admits with correctly evaluated arithmetic.
    """
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        day_start = datetime.now(UTC).date()

        # Part 2a: Empty table + limit exceeded on first request
        # Set entitlement limit = 1 call. Since generation requires 2 calls, conditional INSERT must fail.
        repo = LlmUsageRepository(session)
        repo.upsert_entitlement(
            user_id=user_model.id,
            tier="free",
            daily_call_limit=1,
            monthly_call_limit=10,
        )
        session.commit()

    provider = FakePipelineProvider()
    svc, _ = _build_test_service(pg_session_factory, provider)

    with pytest.raises(LlmQuotaExceededError) as exc_info:
        svc.generate(GenerateRequest(prompt="first call on empty table"), current_user)
    assert exc_info.value.scope == "user"
    assert exc_info.value.window_kind == "day"

    # Counter row should NOT exist because conditional INSERT refused it
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        counter = repo.get_counter(
            scope="user",
            scope_key=str(user_model.id),
            window_kind="day",
            window_start=day_start,
        )
        assert counter is None

    # Part 2b: Empty table + valid first request
    with pg_session_factory() as session:
        user_model_2, current_user_2 = _create_user(session)
        repo = LlmUsageRepository(session)
        repo.upsert_entitlement(
            user_id=user_model_2.id,
            tier="developer",
            daily_call_limit=10,
            monthly_call_limit=100,
        )
        session.commit()

    provider2 = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("admitted");', model="gen", prompt_tokens=20, completion_tokens=10),
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("second admitted");', model="gen", prompt_tokens=20, completion_tokens=10),
        ]
    )
    svc2, _ = _build_test_service(pg_session_factory, provider2)

    # First request
    res1 = svc2.generate(GenerateRequest(prompt="call 1"), current_user_2)
    assert res1.content == 'console.log("admitted");'

    # Verify counter has non-null initialized values
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
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

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        counter2 = repo.get_counter(
            scope="user",
            scope_key=str(user_model_2.id),
            window_kind="day",
            window_start=day_start,
        )
        assert counter2.calls_settled == 4
        assert counter2.calls_reserved == 4


def test_3_settlement_is_idempotent(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """3. Settlement is idempotent — settling twice counts once."""
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        repo = LlmUsageRepository(session)
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
        session.commit()
        res_id = res.id
        user_id = user_model.id

    _, usage_svc = _build_test_service(pg_session_factory, FakePipelineProvider())

    response = LlmProviderResponse(
        text="output",
        model="openrouter/free",
        prompt_tokens=100,
        completion_tokens=50,
    )

    # First settle
    usage_svc.settle_call(
        reservation_id=res_id,
        user_id=user_id,
        request_id=req_id,
        call_kind="generator",
        provider_name="openrouter",
        model_id="openrouter/free",
        response=response,
        status="ok",
    )

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        counter1 = repo.get_counter(
            scope="user",
            scope_key=str(user_id),
            window_kind="day",
            window_start=day_start,
        )
        assert counter1.calls_settled == 1
        assert counter1.calls_reserved == 1
        events1 = repo.get_events_by_request_id(req_id)
        assert len(events1) == 1

    # Second settle (retried)
    usage_svc.settle_call(
        reservation_id=res_id,
        user_id=user_id,
        request_id=req_id,
        call_kind="generator",
        provider_name="openrouter",
        model_id="openrouter/free",
        response=response,
        status="ok",
    )

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        counter2 = repo.get_counter(
            scope="user",
            scope_key=str(user_id),
            window_kind="day",
            window_start=day_start,
        )
        assert counter2.calls_settled == 1
        assert counter2.calls_reserved == 1
        events2 = repo.get_events_by_request_id(req_id)
        assert len(events2) == 1


def test_4_pre_call_reservation_persistence(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """4. Pre-call reservation persistence: committed before provider call and survives worker crash.

    Demonstrated via real subprocess termination (os._exit(42)) mid-call against PostgreSQL.
    """
    db_url = getattr(pg_session_factory, "db_url")
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        user_id = user_model.id

    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=_worker_crash_mid_call, args=(db_url, user_id))
    p.start()
    p.join(timeout=10)

    assert p.exitcode == 42, f"Expected worker to exit with 42, got {p.exitcode}"

    # Check that reservations survived in database after unhandled crash
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        reservations = repo.get_reservations_by_user_id(user_id)
        assert len(reservations) == 2
        states = {r.call_kind: r.state for r in reservations}
        assert states.get("guard") == "started"
        assert states.get("generator") == "reserved"


def test_5_cost_ceiling_enforcement(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """5. Cost ceiling enforcement: refuses request whose reserved upper bound exceeds ceiling."""
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)

    # Set a tiny global monthly cost ceiling (e.g. 100 micros)
    settings = _make_test_settings(
        llm_global_monthly_cost_ceiling_micros=100,  # 100 micros ceiling
    )
    provider = FakePipelineProvider()
    svc, _ = _build_test_service(pg_session_factory, provider, settings=settings)

    with pytest.raises(LlmQuotaExceededError) as exc_info:
        svc.generate(GenerateRequest(prompt="request exceeding cost ceiling"), current_user)

    assert exc_info.value.scope == "global"
    assert exc_info.value.window_kind == "month"


def test_6_ceiling_binds_after_settlement(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """6. Ceiling binds after settlement: assert next request is refused because cost_micros counts."""
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        repo = LlmUsageRepository(session)
        month_start = datetime.now(UTC).date().replace(day=1)

        # Compute bound for standard request
        settings = _make_test_settings(
            llm_global_monthly_cost_ceiling_micros=100_000,
        )
        provider = FakePipelineProvider()
        svc, usage_svc = _build_test_service(pg_session_factory, provider, settings=settings)

        req = GenerateRequest(prompt="short prompt")
        guard_user_prompt = _build_guard_prompt(req)
        gen_user_prompt = _build_generation_prompt(req)
        total_req_bound = usage_svc.compute_guard_cost_bound(
            len(guard_user_prompt.encode("utf-8"))
        ) + usage_svc.compute_generator_cost_bound(
            len(gen_user_prompt.encode("utf-8"))
        )

        # Settle prior spending into the counter so cost_micros is close to ceiling
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
        session.commit()

    # Now cost_micros + total_req_bound > 100_000
    with pytest.raises(LlmQuotaExceededError) as exc_info:
        svc.generate(req, current_user)

    assert exc_info.value.scope == "global"
    assert exc_info.value.window_kind == "month"


def test_7_downstream_cleanup_by_lifecycle_point(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """7. Downstream cleanup by lifecycle point:

    - Preflight failure: both reservations released, returning all 2 calls.
      Starting at limit - 2, a second complete generation succeeds.
    - Post-start guard failure (rejection, timeout, etc.): guard call consumes 1 call,
      downstream generator is released. Exactly 1 call remains consumed.
      At limit - 2, counter sits at limit - 1 so second generation is refused.
      Seeded at limit - 3, second generation succeeds.
    """
    limit = 10
    day_start = datetime.now(UTC).date()
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        repo = LlmUsageRepository(session)
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
        session.commit()
        user_id = user_model.id

    provider_fail = FakePipelineProvider(
        preflight_error=LlmProviderNotConfiguredError("Provider unconfigured")
    )
    svc_fail, _ = _build_test_service(pg_session_factory, provider_fail)

    with pytest.raises(LlmProviderNotConfiguredError):
        svc_fail.generate(GenerateRequest(prompt="preflight fail"), current_user)

    # Counter should be back to 8 (limit - 2)
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        counter = repo.get_counter(
            scope="user",
            scope_key=str(user_id),
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
    svc_ok, _ = _build_test_service(pg_session_factory, provider_ok)
    res = svc_ok.generate(GenerateRequest(prompt="succeeds now"), current_user)
    assert res.content == 'console.log("recovered");'

    # Case 7b: Post-start guard failure (e.g. guard rejects prompt)
    # Reset counter for a fresh user
    with pg_session_factory() as session:
        user2, cur2 = _create_user(session)
        repo = LlmUsageRepository(session)
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
        session.commit()
        user2_id = user2.id

    guard_reject_provider = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": false}', model="guard", prompt_tokens=10, completion_tokens=5),
        ]
    )
    svc_reject, _ = _build_test_service(pg_session_factory, guard_reject_provider)

    with pytest.raises(PromptRejectedError):
        svc_reject.generate(GenerateRequest(prompt="malicious prompt"), cur2)

    # Exactly 1 call remains consumed (guard settled), and generator was released.
    # So counter sits at limit - 1 (8 + 1 = 9)
    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        counter2 = repo.get_counter(
            scope="user",
            scope_key=str(user2_id),
            window_kind="day",
            window_start=day_start,
        )
        assert counter2.calls_reserved == limit - 1
        assert counter2.calls_settled == 1

    # At limit - 1, a second 2-call generation is refused!
    with pytest.raises(LlmQuotaExceededError):
        svc_reject.generate(GenerateRequest(prompt="retry generation"), cur2)

    # Now verify: seeded at limit - 3, counter sits at limit - 2 after guard failure, so second gen succeeds
    with pg_session_factory() as session:
        user3, cur3 = _create_user(session)
        repo = LlmUsageRepository(session)
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
        session.commit()

    provider3 = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": false}', model="guard", prompt_tokens=10, completion_tokens=5),
            # Second attempt responses:
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("works");', model="gen", prompt_tokens=20, completion_tokens=10),
        ]
    )
    svc3, _ = _build_test_service(pg_session_factory, provider3)

    # First attempt: guard rejects
    with pytest.raises(PromptRejectedError):
        svc3.generate(GenerateRequest(prompt="bad prompt"), cur3)

    # Counter sits at limit - 2 (7 + 1 = 8). Second generation requires 2 calls -> 8 + 2 <= 10 -> succeeds!
    res3 = svc3.generate(GenerateRequest(prompt="good prompt"), cur3)
    assert res3.content == 'console.log("works");'


def test_8_release_returns_both_columns(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """8. Release returns BOTH columns: cost_reserved_micros is returned in addition to calls_reserved."""
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        repo = LlmUsageRepository(session)
        day_start = datetime.now(UTC).date()
        initial_cost = 50_000
        repo.reserve_quota(
            scope="user",
            scope_key=str(user_model.id),
            window_kind="day",
            window_start=day_start,
            call_cost=1,
            cost_reserved_micros=initial_cost,
        )
        session.commit()
        user_id = user_model.id

    _, usage_svc = _build_test_service(pg_session_factory, FakePipelineProvider())

    # Reserve 2 calls with cost bound
    req_id = uuid4()
    guard_id, gen_id = usage_svc.reserve_initial_generation(
        user=current_user,
        request_id=req_id,
        provider="openrouter",
        prompt_bytes=len("hello".encode("utf-8")),
    )

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        counter_after_reserve = repo.get_counter(
            scope="user",
            scope_key=str(user_id),
            window_kind="day",
            window_start=day_start,
        )
        assert counter_after_reserve.calls_reserved == 3
        assert counter_after_reserve.cost_reserved_micros > initial_cost

    # Release both reservations
    usage_svc.release_reservation(reservation_id=guard_id, user_id=user_id)
    usage_svc.release_reservation(reservation_id=gen_id, user_id=user_id)

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        counter_after_release = repo.get_counter(
            scope="user",
            scope_key=str(user_id),
            window_kind="day",
            window_start=day_start,
        )
        assert counter_after_release.calls_reserved == 1
        assert counter_after_release.cost_reserved_micros == initial_cost


@pytest.mark.parametrize(
    "case_name, expected_status, expect_model_in_ledger",
    [
        ("http_error", "provider_error", False),
        ("timeout", "timeout", False),
        ("conn_failure", "provider_error", False),
        ("empty_body", "provider_error", False),
        ("partial_usage", "ok", True),
    ],
)
def test_9_failure_paths_and_partial_usage_keep_full_bound(
    pg_session_factory: sessionmaker[Session],
    case_name: str,
    expected_status: str,
    expect_model_in_ledger: bool,
) -> None:
    """9. Failure paths and partial-usage completions keep full bound.

    Tests real adapter response mapping and pipeline settlement across 5 cases:
    1. HTTP error: OpenRouter returns 500 -> LlmProviderError -> status='provider_error'
    2. Timeout: transport raises TimeoutError -> LlmProviderError (from TimeoutError) -> status='timeout', model_id=None
    3. Connection failure: transport raises ConnectionResetError -> LlmProviderError -> status='provider_error', model_id=None
    4. Unusable body: model returns empty body -> TextGenerationError -> status='provider_error'
    5. Missing/partial usage: model returns valid content with 0 tokens -> status='ok'
    In all cases, the full reserved cost bound is kept on the ledger and PostgreSQL constraints hold.
    """
    def make_transport(cn: str):
        def transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpResponse:
            if cn == "http_error":
                return HttpResponse(500, '{"error": {"message": "Service error"}}', {})
            elif cn == "timeout":
                raise TimeoutError("Socket read timeout")
            elif cn == "conn_failure":
                raise ConnectionResetError("Connection reset by peer")
            elif cn == "empty_body":
                return HttpResponse(
                    200,
                    json.dumps({"choices": [{"message": {"content": ""}}], "model": "openrouter/free"}),
                    {},
                )
            elif cn == "partial_usage":
                return HttpResponse(
                    200,
                    json.dumps({
                        "choices": [{"message": {"content": '{"safe": true}'}}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                        "model": "openrouter/free",
                    }),
                    {},
                )
            raise ValueError(f"Unknown case {cn}")
        return transport

    client = OpenRouterClient(
        api_key="test-key",
        timeout_seconds=1,
        transport=make_transport(case_name),
    )

    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        user_id = user_model.id

    svc, _ = _build_test_service(pg_session_factory, client)

    if case_name == "partial_usage":
        # Guard passes with partial usage, generator also returns partial usage
        def gen_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> HttpResponse:
            # Check if prompt contains guard or generator
            if b"safety evaluator" in body or b"guard" in body:
                return HttpResponse(
                    200,
                    json.dumps({
                        "choices": [{"message": {"content": '{"safe": true}'}}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                        "model": "openrouter/free",
                    }),
                    {},
                )
            return HttpResponse(
                200,
                json.dumps({
                    "choices": [{"message": {"content": 'console.log("ok");'}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                    "model": "openrouter/free",
                }),
                {},
            )
        client_gen = OpenRouterClient(
            api_key="test-key",
            timeout_seconds=1,
            transport=gen_transport,
        )
        svc_gen, _ = _build_test_service(pg_session_factory, client_gen)
        res = svc_gen.generate(GenerateRequest(prompt="test partial"), current_user)
        assert res.content == 'console.log("ok");'
    else:
        with pytest.raises(Exception):
            svc.generate(GenerateRequest(prompt="test outcome"), current_user)

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        reservations = repo.get_reservations_by_user_id(user_id)
        assert len(reservations) == 2, f"Expected 2 reservations for {case_name}, got {len(reservations)}"
        guard_res = next(r for r in reservations if r.call_kind == "guard")
        gen_res = next(r for r in reservations if r.call_kind == "generator")

        events = repo.get_events_by_user_id(user_id)
        now_utc = guard_res.created_at.astimezone(UTC)
        day_start = now_utc.date()
        month_start = day_start.replace(day=1)

        if case_name == "partial_usage":
            # Case 5: Both guard and generator succeeded with 0 tokens reported.
            # Both reservations must be settled, retaining their full reserved cost bounds.
            assert guard_res.state == "settled"
            assert gen_res.state == "settled"
            assert len(events) == 2, f"Expected 2 events for partial_usage, got {len(events)}"

            guard_event = next(e for e in events if e.reservation_id == guard_res.id)
            gen_event = next(e for e in events if e.reservation_id == gen_res.id)

            assert guard_event.status == "ok"
            assert gen_event.status == "ok"
            # Exact equality: full reserved bounds are preserved on both ledger events
            assert guard_event.estimated_cost_micros == guard_res.cost_reserved_micros
            assert gen_event.estimated_cost_micros == gen_res.cost_reserved_micros
            assert guard_event.model_id == "openrouter/free"
            assert gen_event.model_id == "openrouter/free"

            expected_total_cost = guard_res.cost_reserved_micros + gen_res.cost_reserved_micros
            for scope, key in [("user", str(user_id)), ("global", "-")]:
                for wk, ws in [("day", day_start), ("month", month_start)]:
                    c = repo.get_counter(scope=scope, scope_key=key, window_kind=wk, window_start=ws)
                    assert c is not None, f"Counter missing for {scope}/{wk} in {case_name}"
                    assert c.calls_reserved == 2
                    assert c.calls_settled == 2
                    assert c.cost_reserved_micros == 0
                    assert c.cost_micros == expected_total_cost, (
                        f"Counter cost_micros {c.cost_micros} != expected {expected_total_cost} for {scope}/{wk}"
                    )
        else:
            # Cases 1-4: Guard call failed; guard settled with failure status; generator was released downstream.
            assert guard_res.state == "settled"
            assert gen_res.state == "released"
            assert len(events) == 1, f"Expected exactly 1 event for {case_name}, got {len(events)}"

            guard_event = events[0]
            assert guard_event.reservation_id == guard_res.id
            assert guard_event.status == expected_status, f"Wrong status for {case_name}: {guard_event.status}"
            # Exact equality: full conservative reserved bound is kept on ledger
            assert guard_event.estimated_cost_micros == guard_res.cost_reserved_micros
            if not expect_model_in_ledger:
                assert guard_event.model_id is None
            else:
                assert guard_event.model_id == "openrouter/free"

            # Check all 4 counters: user/day, user/month, global/day, global/month
            for scope, key in [("user", str(user_id)), ("global", "-")]:
                for wk, ws in [("day", day_start), ("month", month_start)]:
                    c = repo.get_counter(scope=scope, scope_key=key, window_kind=wk, window_start=ws)
                    assert c is not None, f"Counter missing for {scope}/{wk} in {case_name}"
                    assert c.calls_reserved == 1, f"calls_reserved mismatch for {scope}/{wk} in {case_name}"
                    assert c.calls_settled == 1, f"calls_settled mismatch for {scope}/{wk} in {case_name}"
                    assert c.cost_reserved_micros == 0, f"cost_reserved_micros must be 0 for {scope}/{wk} in {case_name}"
                    assert c.cost_micros == guard_res.cost_reserved_micros, (
                        f"cost_micros {c.cost_micros} != full bound {guard_res.cost_reserved_micros} for {scope}/{wk} in {case_name}"
                    )


def test_10_concurrent_release_idempotency(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """10. Concurrent release idempotency: two simultaneous releasers resolve through atomic gate.

    Exactly one state change occurs and exactly one counter deduction is made.
    Verified on PostgreSQL without Python locks using threading.Barrier(2).
    """
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        repo = LlmUsageRepository(session)
        day_start = datetime.now(UTC).date()
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
        session.commit()
        res_id = res.id
        user_id = user_model.id

    _, usage_svc = _build_test_service(pg_session_factory, FakePipelineProvider())

    barrier = threading.Barrier(2)

    def release_worker() -> None:
        barrier.wait()
        usage_svc.release_reservation(reservation_id=res_id, user_id=user_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(release_worker)
        f2 = executor.submit(release_worker)
        f1.result()
        f2.result()

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        counter = repo.get_counter(
            scope="user",
            scope_key=str(user_id),
            window_kind="day",
            window_start=day_start,
        )
        # Exactly one deduction across all counters
        assert counter.calls_reserved == 0
        assert counter.cost_reserved_micros == 0

        # Reservation state is released
        r = repo.get_reservation_by_id(res_id)
        assert r is not None
        assert r.state == "released"


def test_11_inactive_limit_dimensions_across_all_four_scope_window_pairs(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """11. Inactive limit dimensions across all four scope/window pairs.

    Executes reservations where only one dimension is configured and the other is NULL:
    - user/day: call_limit set, cost_limit = NULL
    - user/month: call_limit set, cost_limit = NULL
    - global/day: call_limit set, cost_limit = NULL
    - global/month: call_limit = NULL, cost_limit set
    Valid requests are admitted and SQL three-valued logic does not reject them.
    """
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        repo = LlmUsageRepository(session)
        day_start = datetime.now(UTC).date()
        month_start = day_start.replace(day=1)

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
        session.commit()


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


# -----------------------------------------------------------------------------
# Reviewer Feedback Verification Suite (F1, F2, F3, S1, S2, N1)
# -----------------------------------------------------------------------------


def test_f1_window_binding_across_midnight_and_month_boundaries(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """F1: Settlement and release bind to reservation.created_at, not wall-clock time.

    Verifies across September 30 -> October 1 month boundary:
    1. Pre-seeds September 30 and October 1 counters across all 4 keys.
    2. Releasing a September reservation decrements September counters and leaves October unchanged.
    3. Settling a September reservation increments settled in September and leaves October unchanged.
    4. Handles absent October rows without creating ghost rows or failing check constraints.
    """
    sep_time = datetime(2026, 9, 30, 23, 59, 50, tzinfo=UTC)
    sep_day = sep_time.date()
    sep_month = sep_day.replace(day=1)
    oct_day = datetime(2026, 10, 1, 0, 1, 0, tzinfo=UTC).date()
    oct_month = oct_day.replace(day=1)

    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        user_id = user_model.id
        repo = LlmUsageRepository(session)

        # Pre-seed September counters for all 4 keys
        for scope, key in [("user", str(user_id)), ("global", "-")]:
            repo.reserve_quota(scope=scope, scope_key=key, window_kind="day", window_start=sep_day, call_cost=2, cost_reserved_micros=20_000)
            repo.reserve_quota(scope=scope, scope_key=key, window_kind="month", window_start=sep_month, call_cost=2, cost_reserved_micros=20_000)

        # Pre-seed October counters for all 4 keys with existing populated activity
        for scope, key in [("user", str(user_id)), ("global", "-")]:
            repo.reserve_quota(scope=scope, scope_key=key, window_kind="day", window_start=oct_day, call_cost=3, cost_reserved_micros=30_000)
            repo.settle_quota(scope=scope, scope_key=key, window_kind="day", window_start=oct_day, cost_reserved_micros=10_000, settled_cost_micros=5000)
            repo.reserve_quota(scope=scope, scope_key=key, window_kind="month", window_start=oct_month, call_cost=3, cost_reserved_micros=30_000)
            repo.settle_quota(scope=scope, scope_key=key, window_kind="month", window_start=oct_month, cost_reserved_micros=10_000, settled_cost_micros=5000)

        # Create two September reservations (created on Sep 30 23:59:50 UTC)
        res_rel = repo.create_reservation(
            user_id=user_id,
            request_id=uuid4(),
            call_kind="generator",
            provider="openrouter",
            cost_reserved_micros=10_000,
            created_at=sep_time,
        )
        res_set = repo.create_reservation(
            user_id=user_id,
            request_id=uuid4(),
            call_kind="guard",
            provider="openrouter",
            cost_reserved_micros=10_000,
            created_at=sep_time,
        )
        repo.transition_reservation_to_started(res_set.id)
        session.commit()
        rel_id = res_rel.id
        set_id = res_set.id
        set_req_id = res_set.request_id

    _, usage_svc = _build_test_service(pg_session_factory, FakePipelineProvider())

    # Case A: Release September reservation
    usage_svc.release_reservation(reservation_id=rel_id, user_id=user_id)

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        # Verify September counters were decremented across user and global keys
        for scope, key in [("user", str(user_id)), ("global", "-")]:
            for wk, ws in [("day", sep_day), ("month", sep_month)]:
                c_sep = repo.get_counter(scope=scope, scope_key=key, window_kind=wk, window_start=ws)
                assert c_sep.calls_reserved == 1
                assert c_sep.cost_reserved_micros == 10_000

        # Verify October counters remain 100% UNCHANGED across user and global keys
        for scope, key in [("user", str(user_id)), ("global", "-")]:
            for wk, ws in [("day", oct_day), ("month", oct_month)]:
                c_oct = repo.get_counter(scope=scope, scope_key=key, window_kind=wk, window_start=ws)
                assert c_oct.calls_reserved == 3
                assert c_oct.calls_settled == 1
                assert c_oct.cost_reserved_micros == 20_000
                assert c_oct.cost_micros == 5000

    # Case B: Settle September reservation
    response = LlmProviderResponse(text='{"safe": true}', model="openrouter/free", prompt_tokens=10, completion_tokens=5)
    usage_svc.settle_call(
        reservation_id=set_id,
        user_id=user_id,
        request_id=set_req_id,
        call_kind="guard",
        provider_name="openrouter",
        model_id="openrouter/free",
        response=response,
        status="ok",
    )

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        # Verify September counters settled across user and global keys
        for scope, key in [("user", str(user_id)), ("global", "-")]:
            for wk, ws in [("day", sep_day), ("month", sep_month)]:
                c_sep = repo.get_counter(scope=scope, scope_key=key, window_kind=wk, window_start=ws)
                assert c_sep.calls_reserved == 1
                assert c_sep.calls_settled == 1
                assert c_sep.cost_reserved_micros == 0

        # Verify October counters are STILL 100% UNCHANGED across user and global keys
        for scope, key in [("user", str(user_id)), ("global", "-")]:
            for wk, ws in [("day", oct_day), ("month", oct_month)]:
                c_oct = repo.get_counter(scope=scope, scope_key=key, window_kind=wk, window_start=ws)
                assert c_oct.calls_reserved == 3
                assert c_oct.calls_settled == 1
                assert c_oct.cost_reserved_micros == 20_000
                assert c_oct.cost_micros == 5000

    # Case C: Absent October rows: both release and settle do not create October rows
    with pg_session_factory() as session:
        user2, _ = _create_user(session)
        user2_id = user2.id
        repo = LlmUsageRepository(session)
        for scope, key in [("user", str(user2_id)), ("global", "-")]:
            repo.reserve_quota(scope=scope, scope_key=key, window_kind="day", window_start=sep_day, call_cost=2, cost_reserved_micros=20_000)
            repo.reserve_quota(scope=scope, scope_key=key, window_kind="month", window_start=sep_month, call_cost=2, cost_reserved_micros=20_000)
        res_rel_absent = repo.create_reservation(
            user_id=user2_id,
            request_id=uuid4(),
            call_kind="generator",
            provider="openrouter",
            cost_reserved_micros=10_000,
            created_at=sep_time,
        )
        res_set_absent = repo.create_reservation(
            user_id=user2_id,
            request_id=uuid4(),
            call_kind="guard",
            provider="openrouter",
            cost_reserved_micros=10_000,
            created_at=sep_time,
        )
        repo.transition_reservation_to_started(res_set_absent.id)
        session.commit()
        rel_absent_id = res_rel_absent.id
        set_absent_id = res_set_absent.id
        set_absent_req_id = res_set_absent.request_id

    usage_svc.release_reservation(reservation_id=rel_absent_id, user_id=user2_id)
    usage_svc.settle_call(
        reservation_id=set_absent_id,
        user_id=user2_id,
        request_id=set_absent_req_id,
        call_kind="guard",
        provider_name="openrouter",
        model_id="openrouter/free",
        response=response,
        status="ok",
    )

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        # October user rows were never created by release or settle
        for wk, ws in [("day", oct_day), ("month", oct_month)]:
            c_oct_absent = repo.get_counter(scope="user", scope_key=str(user2_id), window_kind=wk, window_start=ws)
            assert c_oct_absent is None


def test_f2_wire_prompt_sizing_and_guard_max_tokens_cap(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """F2: Wire prompt size includes context/code/title, and guard output tokens are capped at guard_output_tokens_max."""
    # 1. Verify that full prompt with context, edit base code, and cell title is measured
    large_context = [LlmContextCell(kind="code", source="c" * 8000)]
    req = GenerateRequest(
        prompt="x" * 100,
        context=large_context,
        base_code="b" * 8000,
        notebook_title="My Notebook Title",
        mode="edit",
    )
    gen_user_prompt = _build_generation_prompt(req)
    prompt_bytes = len(gen_user_prompt.encode("utf-8"))
    assert prompt_bytes > 16_000
    assert "My Notebook Title" in gen_user_prompt

    # 2. Cost ceiling enforcement: a ceiling of 50,000 micros allows a small 100-byte prompt
    # (~35,000 micros) but REFUSES this full 16,000-byte edit prompt (>70,000 micros bound).
    settings = _make_test_settings(
        llm_global_monthly_cost_ceiling_micros=50_000,
        llm_guard_output_tokens_max=75,  # Custom non-default cap
    )
    provider = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            LlmProviderResponse(text='console.log("ok");', model="gen", prompt_tokens=20, completion_tokens=10),
        ]
    )
    svc, usage_svc = _build_test_service(pg_session_factory, provider, settings=settings)

    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)

    # Large request is refused by ceiling
    with pytest.raises(LlmQuotaExceededError) as exc_info:
        svc.generate(req, current_user)
    assert exc_info.value.scope == "global"
    assert exc_info.value.window_kind == "month"

    # Small request succeeds under same 50,000 micros ceiling
    small_req = GenerateRequest(prompt="hello")
    res = svc.generate(small_req, current_user)
    assert res.content == 'console.log("ok");'

    # Guard call passed custom max_tokens=75, not 100 or 256
    assert len(provider.calls) == 2
    assert provider.calls[0]["max_tokens"] == 75


def test_f3_entitlement_zero_limits_and_expiration(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """F3: daily=0 or monthly=0 is strictly preserved, and expired entitlement falls back."""
    with pg_session_factory() as session:
        user1, cur1 = _create_user(session)
        user2, cur2 = _create_user(session)
        user3, cur3 = _create_user(session)
        repo = LlmUsageRepository(session)

        # User 1: daily=0, monthly=None
        repo.upsert_entitlement(
            user_id=user1.id,
            tier="developer",
            daily_call_limit=0,
            monthly_call_limit=None,
        )
        # User 2: daily=None, monthly=0
        repo.upsert_entitlement(
            user_id=user2.id,
            tier="developer",
            daily_call_limit=None,
            monthly_call_limit=0,
        )
        # User 3: daily=0, but valid_until in the past (expired)
        past_time = datetime(2020, 1, 1, tzinfo=UTC)
        repo.upsert_entitlement(
            user_id=user3.id,
            tier="developer",
            daily_call_limit=0,
            monthly_call_limit=0,
            valid_until=past_time,
        )
        session.commit()

    _, usage_svc = _build_test_service(pg_session_factory, FakePipelineProvider())

    with pg_session_factory() as session:
        # Check user 1 limits: daily must be 0, not default!
        d1, m1 = usage_svc.resolve_user_limits(user1.id, cur1.email, session)
        assert d1 == 0
        assert m1 == usage_svc.settings.llm_dev_tier_monthly_calls

        # Check user 2 limits: monthly must be 0, not default!
        d2, m2 = usage_svc.resolve_user_limits(user2.id, cur2.email, session)
        assert d2 == usage_svc.settings.llm_dev_tier_daily_calls
        assert m2 == 0

        # Check user 3 limits: expired entitlement falls back to free tier defaults (not on dev allowlist)
        d3, m3 = usage_svc.resolve_user_limits(user3.id, cur3.email, session)
        assert d3 == usage_svc.settings.llm_free_tier_daily_calls
        assert m3 == usage_svc.settings.llm_free_tier_monthly_calls

    # Now verify that User 1 generation is refused with daily quota exceeded
    provider = FakePipelineProvider()
    svc, _ = _build_test_service(pg_session_factory, provider)
    with pytest.raises(LlmQuotaExceededError) as exc_info:
        svc.generate(GenerateRequest(prompt="hello"), cur1)
    assert exc_info.value.scope == "user"
    assert exc_info.value.window_kind == "day"

    # User 2 generation is refused with monthly quota exceeded
    with pytest.raises(LlmQuotaExceededError) as exc_info2:
        svc.generate(GenerateRequest(prompt="hello"), cur2)
    assert exc_info2.value.scope == "user"
    assert exc_info2.value.window_kind == "month"


def test_s1_provider_timeout_records_timeout_status(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """S1: Provider timeout causes status='timeout' and model_id=None on usage ledger."""
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)

    # Class name intentionally does NOT contain 'timeout' to prove cause-inspection logic
    class NetworkHangError(Exception):
        pass

    timeout_exc = NetworkHangError("Connection dropped by gateway")
    timeout_exc.__cause__ = TimeoutError("Socket read timeout")

    provider = FakePipelineProvider(
        responses=[
            LlmProviderResponse(text='{"safe": true}', model="guard", prompt_tokens=10, completion_tokens=5),
            timeout_exc,
        ]
    )
    svc, _ = _build_test_service(pg_session_factory, provider)

    with pytest.raises(NetworkHangError):
        svc.generate(GenerateRequest(prompt="test timeout"), current_user)

    with pg_session_factory() as session:
        repo = LlmUsageRepository(session)
        events = repo.get_events_by_user_id(user_model.id)
        generator_events = [e for e in events if e.call_kind == "generator"]
        assert len(generator_events) == 1
        assert generator_events[0].status == "timeout"
        assert generator_events[0].model_id is None


def test_s2_start_call_on_released_reservation_raises_error(
    pg_session_factory: sessionmaker[Session],
) -> None:
    """S2: start_call on a reservation that was already released raises LlmServiceError."""
    with pg_session_factory() as session:
        user_model, current_user = _create_user(session)
        repo = LlmUsageRepository(session)
        res = repo.create_reservation(
            user_id=user_model.id,
            request_id=uuid4(),
            call_kind="generator",
            provider="openrouter",
            cost_reserved_micros=5000,
        )
        session.commit()
        res_id = res.id

    _, usage_svc = _build_test_service(pg_session_factory, FakePipelineProvider())

    # Release it first
    usage_svc.release_reservation(reservation_id=res_id, user_id=user_model.id)

    # Now attempt to start it
    provider = FakePipelineProvider()
    with pytest.raises(LlmServiceError, match="Reservation cannot be started"):
        usage_svc.start_call(
            reservation_id=res_id,
            provider=provider,
            model_id="openrouter/free",
            user_id=user_model.id,
        )


def test_n1_validation_error_truncation_exact_cap() -> None:
    """N1: Truncation ensures the entire string including marker never exceeds max_bytes, even for small cap."""
    long_error = "Error: " + ("a" * 500)
    unicode_error = "Ошибка: " + ("щ" * 200)

    # Test caps: 0, 1, 5, 11, 12, 100
    for max_bytes in [0, 1, 5, 11, 12, 100]:
        res_ascii = _truncate_validation_error(long_error, max_bytes)
        assert len(res_ascii.encode("utf-8")) <= max_bytes, f"Exceeded max_bytes={max_bytes} for ASCII"

        res_uni = _truncate_validation_error(unicode_error, max_bytes)
        assert len(res_uni.encode("utf-8")) <= max_bytes, f"Exceeded max_bytes={max_bytes} for Unicode"

        if max_bytes >= 12:
            assert res_ascii.endswith(" [truncated]")

    # Short error remains unchanged
    short_error = "SyntaxError: missing semicolon"
    assert _truncate_validation_error(short_error, 100) == short_error


def test_f6_database_url_not_used_without_test_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """F6: DATABASE_URL is never used for testing without explicit TEST_DATABASE_URL opt-in."""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://should_never_connect.invalid:5432/not_a_test_db")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    from tests.conftest import _resolve_postgres_cluster_url
    url = _resolve_postgres_cluster_url()
    assert url is None or "should_never_connect.invalid" not in url
