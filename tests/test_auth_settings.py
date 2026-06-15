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
    assert settings.otp_rate_limit_window_seconds == 900
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
        resend_api_key="re_test_key",
        email_from="auth@notebook.example",
    )

    assert settings.placeholder_auth_enabled is False


def test_production_requires_eu_bedrock_inference_profiles() -> None:
    with pytest.raises(ValidationError, match="LLM_BEDROCK_GUARD_MODEL_ID"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="production-secret-value-at-least-32-chars",
            otp_hash_secret="production-otp-hash-secret-at-least-32-chars",
            llm_bedrock_guard_model_id="amazon.nova-micro-v1:0",
        )

    with pytest.raises(ValidationError, match="LLM_BEDROCK_GENERATOR_MODEL_ID"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="production-secret-value-at-least-32-chars",
            otp_hash_secret="production-otp-hash-secret-at-least-32-chars",
            llm_bedrock_generator_model_id="amazon.nova-lite-v1:0",
        )


def test_production_disables_backend_execute() -> None:
    # ENABLE_EXECUTE behind the debug/fallback subprocess runner must not be
    # silently turnable-on in prod: the server refuses to start with the unsafe
    # combination until a hardened runtime exists.
    with pytest.raises(ValidationError, match="ENABLE_EXECUTE"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="production-secret-value-at-least-32-chars",
            otp_hash_secret="production-otp-hash-secret-at-least-32-chars",
            enable_execute=True,
        )

    # Local/dev environments may enable it freely.
    settings = Settings(_env_file=None, enable_execute=True)
    assert settings.enable_execute is True


def test_production_requires_resend_api_key() -> None:
    with pytest.raises(ValidationError, match="RESEND_API_KEY"):
        Settings(
            _env_file=None,
            app_env="production",
            jwt_secret="production-secret-value-at-least-32-chars",
            otp_hash_secret="production-otp-hash-secret-at-least-32-chars",
        )

    settings = Settings(
        _env_file=None,
        app_env="production",
        jwt_secret="production-secret-value-at-least-32-chars",
        otp_hash_secret="production-otp-hash-secret-at-least-32-chars",
        resend_api_key="re_test_key",
        email_from="auth@notebook.example",
    )

    assert settings.resend_api_key == "re_test_key"


def test_production_requires_non_default_email_from() -> None:
    base_kwargs = {
        "_env_file": None,
        "app_env": "production",
        "jwt_secret": "production-secret-value-at-least-32-chars",
        "otp_hash_secret": "production-otp-hash-secret-at-least-32-chars",
        "resend_api_key": "re_test_key",
    }

    # Default EMAIL_FROM is rejected: Resend will reject mail from an
    # unverified example.com sender, surfacing as a delivery failure at
    # request time even though startup validation would otherwise pass.
    with pytest.raises(ValidationError, match="EMAIL_FROM"):
        Settings(**base_kwargs)

    # A value that doesn't look like an email address is also rejected.
    with pytest.raises(ValidationError, match="EMAIL_FROM"):
        Settings(**base_kwargs, email_from="not-an-email")

    settings = Settings(**base_kwargs, email_from="auth@notebook.example")
    assert settings.email_from == "auth@notebook.example"


def test_get_email_service_returns_noop_boundary() -> None:
    settings = Settings(_env_file=None)

    assert isinstance(get_email_service(settings), NoopEmailService)


def test_get_email_service_returns_noop_for_unknown_env_without_resend_key() -> None:
    # An unrecognized APP_ENV is neither local-like nor production-like, so
    # validate_auth_settings does not require RESEND_API_KEY/EMAIL_FROM. The
    # factory must agree and stay on the no-op boundary rather than handing
    # an empty api key to the Resend SDK at request time.
    settings = Settings(_env_file=None, app_env="preview")

    assert settings.is_local_like is False
    assert settings.is_production_like is False
    assert isinstance(get_email_service(settings), NoopEmailService)
