"""Contract tests for real Resend SDK integration with RequestsClient.

Verifies that the Resend SDK correctly interacts with the underlying HTTP transport,
forwards custom timeouts and Authorization headers, parses responses, and transforms
transport/provider failures into EmailDeliveryError.
"""

from datetime import UTC, datetime
from importlib.metadata import version
import json
from typing import Any

from fastapi.testclient import TestClient
import pytest
import requests
import resend

from app.core.config import settings
from app.modules.auth.services import EmailDeliveryError, ResendEmailService
from app.modules.auth.services import email_service as email_service_module


class MockResponse:
    """Mock requests.Response for SDK transport interception."""

    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b'{"id": "re_contract_test_123"}',
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = (
            headers if headers is not None else {"Content-Type": "application/json"}
        )
        self.encoding = "utf-8"

    def json(self) -> Any:
        return json.loads(self.content.decode(self.encoding))


@pytest.fixture(autouse=True)
def restore_resend_globals() -> Any:
    """Restore global Resend SDK attributes after each test."""
    original_api_key = resend.api_key
    original_default_client = resend.default_http_client
    original_api_url = getattr(resend, "api_url", "https://api.resend.com")
    try:
        yield
    finally:
        resend.api_key = original_api_key
        resend.default_http_client = original_default_client
        resend.api_url = original_api_url


def test_rsdk_01_successful_send_forwards_options_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSDK-01: Real SDK forwards timeout, authorization header, and payload to transport."""
    recorded_calls: list[dict[str, Any]] = []

    def fake_request(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> MockResponse:
        recorded_calls.append({"method": method, "url": url, "kwargs": kwargs})
        return MockResponse(
            status_code=200,
            content=b'{"id": "re_contract_success_456"}',
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("resend.http_client_requests.requests.request", fake_request)

    service = ResendEmailService(
        api_key="re_contract_key_123",
        from_email="auth@notebook.example",
        request_timeout_seconds=7,
    )
    expires_at = datetime(2026, 10, 17, 23, 0, tzinfo=UTC)

    service.send_otp(email="recipient@example.com", code="987654", expires_at=expires_at)

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call["method"].lower() == "post"
    assert "/emails" in call["url"]
    assert call["kwargs"].get("timeout") == 7

    headers = call["kwargs"].get("headers")
    assert isinstance(headers, dict)
    assert headers.get("Authorization") == "Bearer re_contract_key_123"

    payload = call["kwargs"].get("json")
    assert isinstance(payload, dict)
    assert payload.get("from") == "auth@notebook.example"
    assert payload.get("to") == "recipient@example.com"
    assert "987654" in str(payload.get("text"))


def test_rsdk_02_timeout_raises_delivery_error_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSDK-02: Transport timeout raises EmailDeliveryError with no hidden retry."""
    recorded_calls: list[dict[str, Any]] = []

    def failing_timeout(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> MockResponse:
        recorded_calls.append({"method": method, "url": url, "kwargs": kwargs})
        raise requests.Timeout("Read timeout after 7 seconds")

    monkeypatch.setattr("resend.http_client_requests.requests.request", failing_timeout)

    service = ResendEmailService(
        api_key="re_contract_key_123",
        from_email="auth@notebook.example",
        request_timeout_seconds=7,
    )

    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="recipient@example.com",
            code="123456",
            expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
        )

    assert len(recorded_calls) == 1


@pytest.mark.xfail(
    version("resend") == "2.30.1",
    raises=pytest.fail.Exception,
    reason="Known Resend 2.30.1 HTTP status handling defect (api#113)",
    strict=True,
)
def test_rsdk_03_http_429_without_status_code_in_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSDK-03: HTTP 429 without statusCode in body raises EmailDeliveryError."""
    def rate_limited(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> MockResponse:
        return MockResponse(
            status_code=429,
            content=b'{"name": "rate_limit_exceeded", "message": "Too many requests"}',
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("resend.http_client_requests.requests.request", rate_limited)

    service = ResendEmailService(
        api_key="re_contract_key_123",
        from_email="auth@notebook.example",
    )

    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="recipient@example.com",
            code="123456",
            expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
        )


def test_rsdk_04_http_422_with_status_code_in_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSDK-04: HTTP 422 with statusCode in body raises EmailDeliveryError."""
    def validation_error(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> MockResponse:
        return MockResponse(
            status_code=422,
            content=(
                b'{"statusCode": 422, "name": "missing_required_field", '
                b'"message": "The field to is required"}'
            ),
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr("resend.http_client_requests.requests.request", validation_error)

    service = ResendEmailService(
        api_key="re_contract_key_123",
        from_email="auth@notebook.example",
    )

    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="recipient@example.com",
            code="123456",
            expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
        )


def test_rsdk_05_http_502_503_html_or_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSDK-05: Gateway errors with HTML or empty body raise EmailDeliveryError."""
    service = ResendEmailService(
        api_key="re_contract_key_123",
        from_email="auth@notebook.example",
    )
    expires_at = datetime(2026, 10, 17, 23, 0, tzinfo=UTC)

    # Subtest A: 502 with text/html
    monkeypatch.setattr(
        "resend.http_client_requests.requests.request",
        lambda *args, **kwargs: MockResponse(
            status_code=502,
            content=b"<html><body>502 Bad Gateway</body></html>",
            headers={"Content-Type": "text/html"},
        ),
    )
    with pytest.raises(EmailDeliveryError):
        service.send_otp(email="recipient@example.com", code="123456", expires_at=expires_at)

    # Subtest B: 503 with empty body and no content-type header
    monkeypatch.setattr(
        "resend.http_client_requests.requests.request",
        lambda *args, **kwargs: MockResponse(
            status_code=503,
            content=b"",
            headers={},
        ),
    )
    with pytest.raises(EmailDeliveryError):
        service.send_otp(email="recipient@example.com", code="123456", expires_at=expires_at)


def test_rsdk_06_http_200_malformed_json_or_invalid_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSDK-06: Malformed response bodies raise EmailDeliveryError."""
    service = ResendEmailService(
        api_key="re_contract_key_123",
        from_email="auth@notebook.example",
    )
    expires_at = datetime(2026, 10, 17, 23, 0, tzinfo=UTC)

    # Subtest A: Invalid JSON string
    monkeypatch.setattr(
        "resend.http_client_requests.requests.request",
        lambda *args, **kwargs: MockResponse(
            status_code=200,
            content=b'{"id": "incomplete json...',
            headers={"Content-Type": "application/json"},
        ),
    )
    with pytest.raises(EmailDeliveryError):
        service.send_otp(email="recipient@example.com", code="123456", expires_at=expires_at)

    # Subtest B: Invalid UTF-8 sequence (invalid continuation byte)
    monkeypatch.setattr(
        "resend.http_client_requests.requests.request",
        lambda *args, **kwargs: MockResponse(
            status_code=200,
            content=b'{"id": "\xc3\x28"}',
            headers={"Content-Type": "application/json"},
        ),
    )
    with pytest.raises(EmailDeliveryError):
        service.send_otp(email="recipient@example.com", code="123456", expires_at=expires_at)


def test_rsdk_07_production_endpoint_handles_success_and_transport_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSDK-07: Production OTP endpoint returns 204 on transport success and 503 on transport failure."""
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "resend_api_key", "re_contract_key_123")
    monkeypatch.setattr(settings, "email_from", "auth@notebook.example")

    # Success path
    monkeypatch.setattr(
        "resend.http_client_requests.requests.request",
        lambda *args, **kwargs: MockResponse(
            status_code=200,
            content=b'{"id": "re_test_endpoint_success"}',
            headers={"Content-Type": "application/json"},
        ),
    )
    success_resp = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": "contract_user@example.com"},
    )
    assert success_resp.status_code == 204
    assert success_resp.content == b""

    # Transport failure path
    monkeypatch.setattr(
        "resend.http_client_requests.requests.request",
        lambda *args, **kwargs: MockResponse(
            status_code=500,
            content=(
                b'{"statusCode": 500, "name": "internal_error", '
                b'"message": "Resend internal outage"}'
            ),
            headers={"Content-Type": "application/json"},
        ),
    )
    fail_resp = client.post(
        f"{settings.api_prefix}/auth/otp/request",
        json={"email": "contract_user_fail@example.com"},
    )
    assert fail_resp.status_code == 503
    assert fail_resp.json()["error"]["code"] == "email_delivery_failed"


def test_rsdk_08_structured_log_omits_code_and_api_key_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSDK-08: Structured logging arguments omit code and api_key fields."""
    monkeypatch.setattr(
        "resend.http_client_requests.requests.request",
        lambda *args, **kwargs: MockResponse(
            status_code=500,
            content=(
                b'{"statusCode": 500, "name": "internal_error", '
                b'"message": "Resend internal server error"}'
            ),
            headers={"Content-Type": "application/json"},
        ),
    )

    logged_events: list[tuple[str, dict[str, Any]]] = []

    class FakeLogger:
        def info(self, event: str, **kwargs: Any) -> None:
            logged_events.append((event, kwargs))

    monkeypatch.setattr(email_service_module, "logger", FakeLogger())

    secret_key = "re_secret_key_123"
    secret_code = "654321"

    service = ResendEmailService(
        api_key=secret_key,
        from_email="auth@notebook.example",
    )

    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="recipient@example.com",
            code=secret_code,
            expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
        )

    assert len(logged_events) == 1
    event, kwargs = logged_events[0]
    assert event == "auth.otp.email.failed"
    assert set(kwargs.keys()) == {"email", "expires_at", "error"}
    assert "code" not in kwargs
    assert "api_key" not in kwargs
    assert secret_code not in repr(kwargs)
    assert secret_key not in repr(kwargs)


def test_rsdk_08_upstream_exception_text_propagated_to_error_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSDK-08 characterization: upstream exception string is recorded in error field verbatim (future sanitization tracked separately)."""
    synthetic_msg = "Upstream transport error: connection reset by peer"

    def failing_request(*args: Any, **kwargs: Any) -> MockResponse:
        raise requests.RequestException(synthetic_msg)

    monkeypatch.setattr("resend.http_client_requests.requests.request", failing_request)

    logged_events: list[tuple[str, dict[str, Any]]] = []

    class FakeLogger:
        def info(self, event: str, **kwargs: Any) -> None:
            logged_events.append((event, kwargs))

    monkeypatch.setattr(email_service_module, "logger", FakeLogger())

    service = ResendEmailService(
        api_key="re_secret_key_123",
        from_email="auth@notebook.example",
    )

    with pytest.raises(EmailDeliveryError):
        service.send_otp(
            email="recipient@example.com",
            code="654321",
            expires_at=datetime(2026, 10, 17, 23, 0, tzinfo=UTC),
        )

    assert len(logged_events) == 1
    _, kwargs = logged_events[0]
    assert synthetic_msg in kwargs["error"]
