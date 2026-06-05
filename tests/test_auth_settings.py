import pytest
from pydantic import ValidationError

from app.core.config import DEV_JWT_SECRET, DEV_OTP_HASH_SECRET, Settings
from app.modules.auth.services import NoopEmailService, get_email_service


def test_auth_settings_defaults_are_local_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.jwt_secret == DEV_JWT_SECRET
    assert settings.otp_hash_secret == DEV_OTP_HASH_SECRET
    assert settings.jwt_access_ttl_seconds == 900
    assert settings.jwt_refresh_ttl_seconds == 2_592_000
    assert settings.otp_ttl_seconds == 300
    assert settings.otp_max_attempts == 5
    assert settings.otp_rate_limit_per_email == 3
    assert settings.is_local_like is True
    assert settings.is_production_like is False
    assert settings.placeholder_auth_enabled is True


def test_production_requires_non_default_jwt_secret() -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None, app_env="production", jwt_secret=DEV_JWT_SECRET)

    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(_env_file=None, app_env="production", jwt_secret="short")


def test_production_requires_non_default_otp_hash_secret() -> None:
    with pytest.raises(ValidationError, match="OTP_HASH_SECRET"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="production-secret-value-at-least-32-chars",
            otp_hash_secret=DEV_OTP_HASH_SECRET,
        )

    with pytest.raises(ValidationError, match="OTP_HASH_SECRET"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="production-secret-value-at-least-32-chars",
            otp_hash_secret="short",
        )


def test_production_disables_placeholder_auth() -> None:
    with pytest.raises(ValidationError, match="ALLOW_PLACEHOLDER_AUTH"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="production-secret-value-at-least-32-chars",
            otp_hash_secret="production-otp-hash-secret-at-least-32-chars",
            allow_placeholder_auth=True,
        )

    settings = Settings(
        _env_file=None,
        app_env="production",
        jwt_secret="production-secret-value-at-least-32-chars",
        otp_hash_secret="production-otp-hash-secret-at-least-32-chars",
    )

    assert settings.placeholder_auth_enabled is False


def test_get_email_service_returns_noop_boundary() -> None:
    settings = Settings(_env_file=None)

    assert isinstance(get_email_service(settings), NoopEmailService)
