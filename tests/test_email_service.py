from datetime import UTC, datetime

import pytest
import resend

from app.core.config import Settings
from app.modules.auth.services import (
    EmailDeliveryError,
    NoopEmailService,
    ResendEmailService,
    get_email_service,
)
from app.modules.auth.services import email_service as email_service_module
from app.modules.auth.services.email_service import (
    extract_domain,
    extract_http_status_code,
    extract_provider_error,
    hash_email,
)


def test_get_email_service_returns_noop_for_local_like_env() -> None:
    config = Settings(_env_file=None, app_env="dev")

    assert isinstance(get_email_service(config), NoopEmailService)


def test_get_email_service_returns_resend_for_production_like_env() -> None:
    config = Settings(
        _env_file=None,
        app_env="production",
        jwt_secret="production-secret-value-at-least-32-chars",
        otp_hash_secret="production-otp-hash-secret-at-least-32-chars",
        resend_api_key="re_test_key",
        email_from="auth@notebook.example",
        llm_openrouter_api_key="test-key",
    )

    assert isinstance(get_email_service(config), ResendEmailService)


def test_resend_email_service_sends_otp_via_resend_sdk(
    monkeypatch: object,
) -> None:
    sent_calls: list[dict[str, object]] = []

    def fake_send(params: dict[str, object]) -> dict[str, str]:
        sent_calls.append(params)
        return {"id": "email_123"}

    monkeypatch.setattr(resend.Emails, "send", fake_send)  # type: ignore[attr-defined]

    service = ResendEmailService(api_key="re_test_key", from_email="auth@notebook.example")
    expires_at = datetime(2026, 6, 11, 10, 5, tzinfo=UTC)

    service.send_otp(email="user@example.com", code="123456", expires_at=expires_at)

    assert resend.api_key == "re_test_key"
    assert len(sent_calls) == 1
    params = sent_calls[0]
    assert params["to"] == "user@example.com"
    assert params["from"] == "auth@notebook.example"
    assert "subject" in params
    assert "123456" in str(params["text"])


def test_resend_email_service_does_not_log_raw_code(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(resend.Emails, "send", lambda params: {"id": "email_123"})  # type: ignore[attr-defined]

    log_calls: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def info(self, event: str, **kwargs: object) -> None:
            log_calls.append((event, kwargs))

    monkeypatch.setattr(email_service_module, "logger", FakeLogger())

    service = ResendEmailService(api_key="re_test_key", from_email="auth@notebook.example")
    service.send_otp(
        email="user@example.com",
        code="123456",
        expires_at=datetime(2026, 6, 11, 10, 5, tzinfo=UTC),
    )

    assert len(log_calls) == 1
    _, kwargs = log_calls[0]
    assert "123456" not in repr(kwargs)


def test_resend_email_service_wraps_provider_errors_and_does_not_log_raw_code(
    monkeypatch: object,
) -> None:
    def failing_send(params: dict[str, object]) -> dict[str, str]:
        raise RuntimeError("Request failed: connection error")

    monkeypatch.setattr(resend.Emails, "send", failing_send)  # type: ignore[attr-defined]

    log_calls: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def info(self, event: str, **kwargs: object) -> None:
            log_calls.append((event, kwargs))

    monkeypatch.setattr(email_service_module, "logger", FakeLogger())

    service = ResendEmailService(api_key="re_test_key", from_email="auth@notebook.example")

    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="user@example.com",
            code="123456",
            expires_at=datetime(2026, 6, 11, 10, 5, tzinfo=UTC),
        )

    assert len(log_calls) == 1
    event, kwargs = log_calls[0]
    assert event == "auth.otp.email.failed"
    assert "123456" not in repr(kwargs)
    assert "user@example.com" not in repr(kwargs)
    assert kwargs["email_hash"] == hash_email("user@example.com")
    assert kwargs["domain"] == "example.com"
    assert kwargs["error_type"] == "RuntimeError"
    assert kwargs["error"] == "Failed to send OTP email: RuntimeError"


def test_hash_email_and_extract_domain_cases() -> None:
    # Stable 12-char SHA-256
    assert hash_email("user@example.com") == "b4c9a289323b"
    assert len(hash_email("user@example.com")) == 12

    # Case-insensitive and whitespace normalization
    assert hash_email("  User@Example.COM  ") == hash_email("user@example.com")

    # Short 1-character local part does not leak local character
    assert len(hash_email("a@example.com")) == 12
    assert "a" != hash_email("a@example.com")

    # Domain extraction
    assert extract_domain("user@example.com") == "example.com"
    assert extract_domain("User@SUB.Example.Com") == "sub.example.com"
    assert extract_domain("not_an_email") == ""
    assert extract_domain("") == ""


def test_resend_email_service_logs_pseudonymized_email_on_success(
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(resend.Emails, "send", lambda params: {"id": "email_123"})  # type: ignore[attr-defined]

    log_calls: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def info(self, event: str, **kwargs: object) -> None:
            log_calls.append((event, kwargs))

    monkeypatch.setattr(email_service_module, "logger", FakeLogger())

    service = ResendEmailService(api_key="re_test_key", from_email="auth@notebook.example")
    service.send_otp(
        email="developer@example.com",
        code="123456",
        expires_at=datetime(2026, 6, 11, 10, 5, tzinfo=UTC),
    )

    assert len(log_calls) == 1
    event, kwargs = log_calls[0]
    assert event == "auth.otp.email.sent"
    assert kwargs["email_hash"] == hash_email("developer@example.com")
    assert kwargs["domain"] == "example.com"
    assert "developer@example.com" not in repr(kwargs)
    assert "123456" not in repr(kwargs)


def test_noop_email_service_logs_plain_email_for_dev_debugging() -> None:
    log_calls: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def info(self, event: str, **kwargs: object) -> None:
            log_calls.append((event, kwargs))

    service = NoopEmailService()
    service_logger = email_service_module.logger
    try:
        email_service_module.logger = FakeLogger()  # type: ignore[assignment]
        service.send_otp(
            email="dev.local@example.com",
            code="654321",
            expires_at=datetime(2026, 6, 11, 10, 5, tzinfo=UTC),
        )
    finally:
        email_service_module.logger = service_logger

    assert len(log_calls) == 1
    event, kwargs = log_calls[0]
    assert event == "auth.otp.email.noop"
    # Plain text email in dev/test is intentional
    assert kwargs["email"] == "dev.local@example.com"
    # Raw OTP code must NEVER be logged even in dev
    assert "654321" not in repr(kwargs)


def test_extract_http_status_code_validation() -> None:
    class DummyException(Exception):
        pass

    # Valid HTTP status codes in standard 100..599 range
    for code in (100, 200, 201, 301, 400, 404, 429, 500, 502, 503, 599):
        exc = DummyException()
        exc.status_code = code
        assert extract_http_status_code(exc) == code

        exc_str = DummyException()
        exc_str.code = str(code)
        assert extract_http_status_code(exc_str) == code

    # Non-HTTP integers (such as 6-digit OTP codes, timestamps, IDs)
    for bad_code in (654321, 123456, 999999, 0, 99, 600, 1000, -1, -500):
        exc = DummyException()
        exc.status_code = bad_code
        assert extract_http_status_code(exc) is None

    # Strings containing credentials, tokens, non-digits, or leading zeros
    for bad_str in (
        "re_secret_key",
        "Bearer token",
        "password",
        "429a",
        "",
        " ",
        "99",
        "600",
        "654321",
        "000429",
        "000100",
        "000599",
        "0429",
        "000000",
        "000",
        "4٢٩",
    ):
        exc = DummyException()
        exc.code = bad_str
        assert extract_http_status_code(exc) is None

    # Fallback to code when status_code is explicitly None
    exc_fallback = DummyException()
    exc_fallback.status_code = None
    exc_fallback.code = 429
    assert extract_http_status_code(exc_fallback) == 429

    exc_fallback_str = DummyException()
    exc_fallback_str.status_code = None
    exc_fallback_str.code = "429"
    assert extract_http_status_code(exc_fallback_str) == 429

    exc_none_both = DummyException()
    exc_none_both.status_code = None
    exc_none_both.code = None
    assert extract_http_status_code(exc_none_both) is None

    # Complex objects (dicts, lists, booleans, None)
    for bad_obj in ({"otp": "123456"}, [400], True, False, None):
        exc = DummyException()
        exc.code = bad_obj
        assert extract_http_status_code(exc) is None

    # Exception with no status attributes
    assert extract_http_status_code(DummyException("generic error")) is None

    # Exception whose properties raise unexpected errors upon access
    class BuggyCodeException(Exception):
        @property
        def status_code(self) -> int:
            raise RuntimeError("broken status_code property")

    assert extract_http_status_code(BuggyCodeException()) is None


def test_extract_provider_error_validation() -> None:
    class DummyException(Exception):
        pass

    # Valid machine-readable provider error tokens
    for token in (
        "rate_limit_exceeded",
        "validation_error",
        "daily_quota_exceeded",
        "internal_server_error",
        "invalid_api_key",
        "missing_required_fields",
    ):
        exc = DummyException()
        exc.error_type = token
        assert extract_provider_error(exc) == token

    # Case normalization
    exc = DummyException()
    exc.error_type = "Rate_Limit_Exceeded"
    assert extract_provider_error(exc) == "rate_limit_exceeded"

    # Reject free-form strings, spaces, punctuation, credentials, or arbitrary text
    for bad_token in (
        "Rate limit exceeded: try again in 60s",
        "re_secret_key_123",
        "password with spaces",
        "comma,separated,values",
        "http://invalid.proxy/",
        "987654321",
        "",
        " ",
        "a" * 65,  # too long
    ):
        exc = DummyException()
        exc.error_type = bad_token
        assert extract_provider_error(exc) is None

    # Reject non-string types
    for bad_type in (123, {"key": "val"}, True, False, None):
        exc = DummyException()
        exc.error_type = bad_type
        assert extract_provider_error(exc) is None

    # Exception whose properties raise unexpected errors upon access
    class BuggyProviderException(Exception):
        @property
        def error_type(self) -> str:
            raise RuntimeError("broken error_type property")

    assert extract_provider_error(BuggyProviderException()) is None


def test_resend_email_service_send_otp_survives_buggy_exception_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Send OTP safely handles buggy upstream exceptions whose attributes raise errors."""
    class BuggySdkException(Exception):
        @property
        def status_code(self) -> int:
            raise RuntimeError("broken status_code")

        @property
        def error_type(self) -> str:
            raise RuntimeError("broken error_type")

    monkeypatch.setattr(
        "resend.Emails.send",
        lambda *args, **kwargs: (_ for _ in ()).throw(BuggySdkException()),
    )

    service = ResendEmailService(
        api_key="re_test_key",
        from_email="auth@notebook.example",
    )

    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="user@example.com",
            code="123456",
            expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
        )


def test_resend_email_service_diagnostic_fallback_does_not_leak_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback logging does not use logger.exception or leak upstream exception secrets."""
    secret_proxy_pass = "SUPER_SECRET_PROXY_PASS_98765"
    upstream_error = ConnectionError(f"Request failed: https://proxy:{secret_proxy_pass}@proxy.host/")

    monkeypatch.setattr(
        "resend.Emails.send",
        lambda *args, **kwargs: (_ for _ in ()).throw(upstream_error),
    )

    warning_events: list[tuple[str, dict]] = []

    class FailingLogger:
        def info(self, *args, **kwargs):
            raise RuntimeError("Primary diagnostic logging failed")

        def warning(self, event, **kwargs):
            warning_events.append((event, kwargs))

    monkeypatch.setattr(email_service_module, "logger", FailingLogger())

    service = ResendEmailService(
        api_key="re_test_key",
        from_email="auth@notebook.example",
    )

    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="user@example.com",
            code="123456",
            expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
        )

    assert len(warning_events) == 1
    event, kwargs = warning_events[0]
    assert event == "auth.otp.email.failed.diagnostic_error"
    assert kwargs.get("diagnostic_error_type") == "RuntimeError"
    # S1: Explicitly assert that exc_info is NOT enabled, preventing structlog traceback emission
    assert "exc_info" not in kwargs or kwargs["exc_info"] is False
    # Ensure raw secret is never present in kwargs or event representation
    assert secret_proxy_pass not in repr(kwargs)
    assert secret_proxy_pass not in repr(warning_events)


def test_resend_email_service_diagnostic_fallback_structlog_json_renderer_emits_no_traceback_or_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real structlog JSONRenderer never emits exception tracebacks or upstream secrets during diagnostic fallback."""
    import io
    import json
    import logging
    import structlog
    from app.core.logging import _build_shared_processors

    secret_proxy_pass = "SUPER_SECRET_PROXY_PASS_RENDERER_TEST"
    upstream_error = ConnectionError(f"Request failed: https://proxy:{secret_proxy_pass}@proxy.host/")

    monkeypatch.setattr(
        "resend.Emails.send",
        lambda *args, **kwargs: (_ for _ in ()).throw(upstream_error),
    )

    # Configure a real structlog logger writing JSON to an in-memory stream
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_build_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler.setFormatter(formatter)
    test_logger = logging.getLogger("test_structlog_fallback")
    test_logger.handlers = [handler]
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    bound_logger = structlog.wrap_logger(test_logger)

    class FailingInfoLogger:
        def info(self, *args, **kwargs):
            raise RuntimeError("Primary diagnostic logging failed")

        def warning(self, event, **kwargs):
            bound_logger.warning(event, **kwargs)

    monkeypatch.setattr(email_service_module, "logger", FailingInfoLogger())

    service = ResendEmailService(
        api_key="re_test_key",
        from_email="auth@notebook.example",
    )

    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="user@example.com",
            code="123456",
            expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
        )

    log_output = log_stream.getvalue().strip()
    assert log_output, "Expected a JSON log line to be emitted"
    log_data = json.loads(log_output)

    # Verify event and safety guarantees
    assert log_data.get("message") == "auth.otp.email.failed.diagnostic_error"
    assert log_data.get("diagnostic_error_type") == "RuntimeError"
    assert "exception" not in log_data
    assert "Traceback" not in log_output
    assert secret_proxy_pass not in log_output


def test_resend_email_service_send_otp_survives_when_all_logging_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both info and warning logging fail, EmailDeliveryError is still guaranteed."""
    monkeypatch.setattr(
        "resend.Emails.send",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("SDK failed")),
    )

    class CompletelyBrokenLogger:
        def info(self, *args, **kwargs):
            raise RuntimeError("Info logger broken")

        def warning(self, *args, **kwargs):
            raise RuntimeError("Warning logger broken")

    monkeypatch.setattr(email_service_module, "logger", CompletelyBrokenLogger())

    service = ResendEmailService(
        api_key="re_test_key",
        from_email="auth@notebook.example",
    )

    # Must raise EmailDeliveryError, not the logger's RuntimeError
    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="user@example.com",
            code="123456",
            expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
        )


def test_resend_email_service_send_otp_survives_logging_failure_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2: If email delivery succeeds via SDK, unexpected logging failure does not fail the operation."""
    # Mock successful Resend API call
    sent_payloads: list[dict] = []
    monkeypatch.setattr(
        "resend.Emails.send",
        lambda payload: sent_payloads.append(payload),
    )

    class BrokenSuccessLogger:
        def info(self, event, **kwargs):
            if event == "auth.otp.email.sent":
                raise RuntimeError("Logger crashed while recording success")

        def warning(self, event, **kwargs):
            pass

    monkeypatch.setattr(email_service_module, "logger", BrokenSuccessLogger())

    service = ResendEmailService(
        api_key="re_test_key",
        from_email="auth@notebook.example",
    )

    # Must NOT raise an exception
    service.send_otp(
        email="user@example.com",
        code="123456",
        expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
    )

    assert len(sent_payloads) == 1
    assert sent_payloads[0]["to"] == "user@example.com"
