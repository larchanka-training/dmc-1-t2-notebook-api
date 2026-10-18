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

    # Strings containing credentials, tokens, or non-digits
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
    ):
        exc = DummyException()
        exc.code = bad_str
        assert extract_http_status_code(exc) is None

    # Complex objects (dicts, lists, booleans, None)
    for bad_obj in ({"otp": "123456"}, [400], True, False, None):
        exc = DummyException()
        exc.code = bad_obj
        assert extract_http_status_code(exc) is None

    # Exception with no status attributes
    assert extract_http_status_code(DummyException("generic error")) is None


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
