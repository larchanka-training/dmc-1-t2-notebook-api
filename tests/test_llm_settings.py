"""Tests for LLM-related Settings validators (A5, A6)."""

import pytest

from app.core.config import Settings


def _base_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    base = {
        "APP_ENV": "dev",
        "JWT_SECRET": "x" * 32,
        "OTP_HASH_SECRET": "y" * 32,
    }
    if overrides:
        base.update(overrides)
    return base


def test_app_version_matches_package_version() -> None:
    """A6: Settings.app_version must equal the pyproject package version."""
    # When pyproject.toml is bumped, this test catches the drift before
    # OpenAPI exports a stale contract version.
    import tomllib
    from pathlib import Path

    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text())
    package_version = pyproject["project"]["version"]

    settings = Settings(_env_file=None)
    assert settings.app_version == package_version, (
        f"pyproject.toml has {package_version}, "
        f"Settings.app_version has {settings.app_version} — bump both."
    )


def test_settings_rejects_retry_budget_above_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """A5: docs/ai-architecture.md §7.1 caps total attempts at 3."""
    for key, value in _base_env({"LLM_VALIDATION_MAX_RETRIES": "5"}).items():
        monkeypatch.setenv(key, value)
    # Also avoid loading the local .env which may set other values.
    with pytest.raises(ValueError, match="LLM_VALIDATION_MAX_RETRIES must be <= 2"):
        Settings(_env_file=None)


def test_settings_accepts_retry_budget_within_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _base_env({"LLM_VALIDATION_MAX_RETRIES": "2"}).items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.llm_validation_max_retries == 2


def test_settings_rejects_non_eu_model_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production-like envs with Bedrock must use EU Geo inference profiles (eu.* prefix)."""
    for key, value in _base_env(
        {
            "APP_ENV": "production",
            "LLM_PROVIDER": "bedrock",
            "LLM_BEDROCK_GENERATOR_MODEL_ID": "amazon.nova-lite-v1:0",
        }
    ).items():
        monkeypatch.setenv(key, value)
    # Production also requires non-default JWT/OTP secrets, already covered
    # by _base_env.
    with pytest.raises(ValueError, match="LLM_BEDROCK_GENERATOR_MODEL_ID"):
        Settings(_env_file=None)


def test_settings_skip_eu_check_allows_non_eu_model_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM_BEDROCK_SKIP_EU_CHECK=true bypasses the eu. prefix requirement."""
    for key, value in _base_env(
        {
            "APP_ENV": "production",
            "LLM_PROVIDER": "bedrock",
            "LLM_BEDROCK_SKIP_EU_CHECK": "true",
            "LLM_BEDROCK_REGION": "us-east-1",
            "LLM_BEDROCK_GENERATOR_MODEL_ID": "amazon.nova-lite-v1:0",
            "LLM_BEDROCK_GUARD_MODEL_ID": "amazon.nova-micro-v1:0",
            "RESEND_API_KEY": "re_test",
            "EMAIL_FROM": "test@example.com",
        }
    ).items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.llm_bedrock_skip_eu_check is True
    assert settings.llm_bedrock_region == "us-east-1"


def test_settings_openrouter_in_production_requires_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production-like envs with OpenRouter must provide an API key."""
    for key, value in _base_env(
        {
            "APP_ENV": "production",
            "RESEND_API_KEY": "re_test",
            "EMAIL_FROM": "test@example.com",
            "LLM_PROVIDER": "openrouter",
            "LLM_OPENROUTER_API_KEY": "",
        }
    ).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(
        ValueError, match="LLM_OPENROUTER_API_KEY must be set in production-like environments"
    ):
        Settings(_env_file=None)


def test_settings_openrouter_in_production_with_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production-like envs with OpenRouter accept a valid API key."""
    for key, value in _base_env(
        {
            "APP_ENV": "production",
            "RESEND_API_KEY": "re_test",
            "EMAIL_FROM": "test@example.com",
            "LLM_PROVIDER": "openrouter",
            "LLM_OPENROUTER_API_KEY": "sk-or-v1-test-key",
        }
    ).items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.normalized_llm_provider == "openrouter"
    assert settings.llm_openrouter_api_key == "sk-or-v1-test-key"


def test_settings_openrouter_with_llm_summary_requires_eu_bedrock_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If summary strategy is 'llm', Bedrock generator model must use EU prefix in production."""
    for key, value in _base_env(
        {
            "APP_ENV": "production",
            "RESEND_API_KEY": "re_test",
            "EMAIL_FROM": "test@example.com",
            "LLM_PROVIDER": "openrouter",
            "LLM_OPENROUTER_API_KEY": "sk-or-v1-test-key",
            "LLM_CONTEXT_SUMMARY_STRATEGY": "llm",
            "LLM_BEDROCK_GENERATOR_MODEL_ID": "us.amazon.nova-lite-v1:0",
        }
    ).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(
        ValueError,
        match="LLM_BEDROCK_GENERATOR_MODEL_ID must use an EU Geo inference profile",
    ):
        Settings(_env_file=None)


def test_settings_openrouter_with_compact_oldest_summary_allows_non_eu_bedrock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Bedrock is completely unused (openrouter + compact-oldest), non-EU bedrock models are ignored."""
    for key, value in _base_env(
        {
            "APP_ENV": "production",
            "RESEND_API_KEY": "re_test",
            "EMAIL_FROM": "test@example.com",
            "LLM_PROVIDER": "openrouter",
            "LLM_OPENROUTER_API_KEY": "sk-or-v1-test-key",
            "LLM_CONTEXT_SUMMARY_STRATEGY": "compact-oldest",
            "LLM_BEDROCK_GENERATOR_MODEL_ID": "us.amazon.nova-lite-v1:0",
            "LLM_BEDROCK_GUARD_MODEL_ID": "us.amazon.nova-micro-v1:0",
        }
    ).items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)
    assert settings.normalized_llm_provider == "openrouter"
    assert settings.llm_context_summary_strategy == "compact-oldest"

