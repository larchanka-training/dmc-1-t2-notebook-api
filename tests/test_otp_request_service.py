from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.auth.repositories import OtpRepository
from app.modules.auth.services import OtpCodeService, OtpRequestService


class CapturingEmailService:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    def send_otp(self, *, email: str, code: str, expires_at: datetime) -> None:
        self.sent.append({"email": email, "code": code, "expires_at": expires_at})


class StaticOtpCodeService(OtpCodeService):
    def __init__(self, code: str) -> None:
        super().__init__()
        self._code = code

    def generate_otp(self) -> str:
        return self._code


def test_otp_request_service_creates_hash_and_sends_email(
    db_session: Session,
) -> None:
    repository = OtpRepository(db_session)
    email_service = CapturingEmailService()
    code_service = StaticOtpCodeService("123456")
    config = Settings(_env_file=None, otp_ttl_seconds=300)
    service = OtpRequestService(
        otp_repository=repository,
        email_service=email_service,
        code_service=code_service,
        config=config,
    )
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)

    result = service.request_otp(email="  USER@Example.COM ", now=now)

    assert result.email == "user@example.com"
    assert result.expires_at == now + timedelta(seconds=300)
    assert result.raw_code == "123456"
    assert result.otp.email == "user@example.com"
    assert result.otp.otp_hash != "123456"
    assert code_service.verify_otp("123456", result.otp.otp_hash)
    assert email_service.sent == [
        {
            "email": "user@example.com",
            "code": "123456",
            "expires_at": now + timedelta(seconds=300),
        }
    ]


def test_otp_request_service_invalidates_previous_active_otps(
    db_session: Session,
) -> None:
    repository = OtpRepository(db_session)
    first_email_service = CapturingEmailService()
    second_email_service = CapturingEmailService()
    config = Settings(_env_file=None)
    now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
    first = OtpRequestService(
        otp_repository=repository,
        email_service=first_email_service,
        code_service=StaticOtpCodeService("111111"),
        config=config,
    ).request_otp(email="user@example.com", now=now)

    second = OtpRequestService(
        otp_repository=repository,
        email_service=second_email_service,
        code_service=StaticOtpCodeService("222222"),
        config=config,
    ).request_otp(email="user@example.com", now=now + timedelta(seconds=1))

    db_session.refresh(first.otp)
    db_session.refresh(second.otp)

    assert first.otp.used_at is not None
    assert second.otp.used_at is None
    assert repository.get_latest_active_by_email(
        "user@example.com", now + timedelta(seconds=1)
    ) == second.otp


def test_otp_request_service_hides_raw_code_in_production_like_config(
    db_session: Session,
) -> None:
    service = OtpRequestService(
        otp_repository=OtpRepository(db_session),
        email_service=CapturingEmailService(),
        code_service=StaticOtpCodeService("123456"),
        config=Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="production-secret-value-at-least-32-chars",
            otp_hash_secret="production-otp-hash-secret-at-least-32-chars",
            resend_api_key="re_test_key",
            email_from="auth@notebook.example",
        ),
    )

    result = service.request_otp(
        email="user@example.com",
        now=datetime(2026, 6, 3, 10, 0, tzinfo=UTC),
    )

    assert result.raw_code is None
