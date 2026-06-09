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
    """Production-like envs must use EU Geo inference profiles (eu.* prefix)."""
    for key, value in _base_env(
        {
            "APP_ENV": "production",
            "LLM_BEDROCK_GENERATOR_MODEL_ID": "amazon.nova-lite-v1:0",
        }
    ).items():
        monkeypatch.setenv(key, value)
    # Production also requires non-default JWT/OTP secrets, already covered
    # by _base_env.
    with pytest.raises(ValueError, match="LLM_BEDROCK_GENERATOR_MODEL_ID"):
        Settings(_env_file=None)
