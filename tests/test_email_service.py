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
