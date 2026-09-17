"""Tests for LLM usage controls and quota settings in Settings."""

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


def test_llm_usage_settings_defaults() -> None:
    """Verify default values for LLM usage controls and quotas."""
    settings = Settings(_env_file=None)
    assert settings.llm_free_tier_daily_calls == 20
    assert settings.llm_free_tier_monthly_calls == 200
    assert settings.llm_dev_tier_daily_calls == 100
    assert settings.llm_dev_tier_monthly_calls == 1000
    assert settings.llm_global_daily_calls == 1000
    assert settings.llm_global_monthly_cost_ceiling_micros == 100_000_000
    assert settings.llm_worst_case_price_micros_prompt == 5_000
    assert settings.llm_worst_case_price_micros_completion == 15_000
    assert settings.llm_system_prompt_allowance_tokens == 1_000
    assert settings.llm_guard_output_tokens_max == 100


@pytest.mark.parametrize(
    ("env_key", "env_val", "expected_msg"),
    [
        ("LLM_FREE_TIER_DAILY_CALLS", "0", "LLM_FREE_TIER_DAILY_CALLS must be positive"),
        ("LLM_FREE_TIER_MONTHLY_CALLS", "0", "LLM_FREE_TIER_MONTHLY_CALLS must be positive"),
        (
            "LLM_FREE_TIER_MONTHLY_CALLS",
            "10",  # less than daily (20)
            "LLM_FREE_TIER_MONTHLY_CALLS must be greater than or equal to",
        ),
        ("LLM_DEV_TIER_DAILY_CALLS", "0", "LLM_DEV_TIER_DAILY_CALLS must be positive"),
        ("LLM_DEV_TIER_MONTHLY_CALLS", "0", "LLM_DEV_TIER_MONTHLY_CALLS must be positive"),
        (
            "LLM_DEV_TIER_MONTHLY_CALLS",
            "50",  # less than daily (100)
            "LLM_DEV_TIER_MONTHLY_CALLS must be greater than or equal to",
        ),
        ("LLM_GLOBAL_DAILY_CALLS", "0", "LLM_GLOBAL_DAILY_CALLS must be positive"),
        (
            "LLM_GLOBAL_MONTHLY_COST_CEILING_MICROS",
            "0",
            "LLM_GLOBAL_MONTHLY_COST_CEILING_MICROS must be positive",
        ),
        (
            "LLM_WORST_CASE_PRICE_MICROS_PROMPT",
            "0",
            "LLM_WORST_CASE_PRICE_MICROS_PROMPT must be positive",
        ),
        (
            "LLM_WORST_CASE_PRICE_MICROS_COMPLETION",
            "0",
            "LLM_WORST_CASE_PRICE_MICROS_COMPLETION must be positive",
        ),
        (
            "LLM_SYSTEM_PROMPT_ALLOWANCE_TOKENS",
            "0",
            "LLM_SYSTEM_PROMPT_ALLOWANCE_TOKENS must be positive",
        ),
        (
            "LLM_GUARD_OUTPUT_TOKENS_MAX",
            "0",
            "LLM_GUARD_OUTPUT_TOKENS_MAX must be positive",
        ),
    ],
)
def test_llm_usage_settings_validations(
    monkeypatch: pytest.MonkeyPatch, env_key: str, env_val: str, expected_msg: str
) -> None:
    """Settings validators must reject invalid values for quota and cost parameters."""
    for key, value in _base_env({env_key: env_val}).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=expected_msg):
        Settings(_env_file=None)
