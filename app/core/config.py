"""Centralised application settings.

Все переменные окружения, флаги фичей и параметры подключения собраны
в одном месте — в классе :class:`Settings`. Pydantic читает их из
``.env`` (или из реального окружения), валидирует типы и кэширует в
синглтоне :data:`settings`, который импортируют все модули.

Конвенция: значение по умолчанию = безопасный dev-default. Любая
prod-ценная вещь (DB URL, OAuth-секрет, JSON-логи) должна быть
перекрыта переменной окружения при деплое.
"""

import re

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-only-jwt-secret-change-me-32-bytes-minimum"
DEV_OTP_HASH_SECRET = "dev-only-otp-hash-secret-change-me-32-bytes"
DEV_EMAIL_FROM = "noreply@example.com"
LOCAL_ENVS = {"dev", "local", "test"}
PRODUCTION_ENVS = {"production", "prod", "staging"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Valid LLM_CONTEXT_SUMMARY_STRATEGY ids — mirrors the strategies registered in
# app/modules/ai_context/services/summary.py (kept here to avoid an import cycle;
# validated at startup so a typo fails fast, not on the first ai-context call).
ALLOWED_SUMMARY_STRATEGIES = {"compact-oldest", "llm"}


class Settings(BaseSettings):
    """Strongly-typed settings loaded from environment / ``.env``.

    Конфиг приложения. Pydantic подтягивает значения из переменных
    окружения (имена совпадают с атрибутами в верхнем регистре),
    падает с понятной ошибкой при несовпадении типов.

    Notes:
        Поле ``app_env`` управляет dev-only поведением (placeholder
        auth, dev-seed в Liquibase). В prod должно быть выставлено
        в ``"production"`` или ``"staging"``.
    """

    app_name: str = "JS Notebook API"
    app_version: str = "0.2.0"
    app_env: str = "dev"
    api_prefix: str = "/api/v1"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    database_url: str = "postgresql://postgres:postgres@localhost:5432/notebook_dev"
    database_echo: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    oauth_name_application_id: str = "change-me"
    oauth_name_secret_key: str = "change-me"
    token_ttl_seconds: int = 86400
    session_ttl_seconds: int = 604800
    jwt_secret: str = DEV_JWT_SECRET
    otp_hash_secret: str = DEV_OTP_HASH_SECRET
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 2_592_000
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_rate_limit_per_email: int = 3
    allow_placeholder_auth: bool | None = None
    resend_api_key: str = ""
    resend_request_timeout_seconds: int = 10
    email_from: str = DEV_EMAIL_FROM
    llm_bedrock_region: str = "eu-north-1"
    llm_bedrock_guard_model_id: str = "eu.amazon.nova-micro-v1:0"
    llm_bedrock_generator_model_id: str = "eu.amazon.nova-lite-v1:0"
    llm_max_prompt_bytes: int = 8_192
    llm_max_total_bytes: int = 16_384
    llm_request_timeout_seconds: int = 30
    llm_rate_limit_per_minute: int = 20
    llm_validation_max_retries: int = 2
    llm_validation_timeout_seconds: float = 5.0
    llm_esbuild_command: str = "esbuild"
    llm_max_tokens: int = 2_048
    llm_temperature: float = 0.2
    # AI context persistence + summary (Epic 07 / #116). The notebook context
    # built on the FE is persisted server-side and rolled up by a pluggable,
    # budget-aware summary service. See docs/ai-architecture.md §4.3.
    # Strategy id for the summary service; switch implementations via env without
    # touching call sites. Resolved by build_summary_service(); unknown → error.
    llm_context_summary_strategy: str = "compact-oldest"
    # Backend code-execution endpoint (POST /api/v1/execute). Disabled by
    # default: it is a debug/fallback runner, not the production sandbox.
    # See docs/execution-architecture.md §12. The subprocess runner is NOT a
    # production-grade sandbox — keep ENABLE_EXECUTE=false unless a hardened
    # runtime is in place. validate_auth_settings hard-rejects enabling it in
    # production-like environments (no silent prod RCE behind a single flag).
    enable_execute: bool = False
    execute_node_command: str = "node"
    execute_default_timeout_ms: int = 5_000
    execute_max_timeout_ms: int = 15_000
    execute_max_code_bytes: int = 262_144
    execute_max_output_bytes: int = 1_048_576
    execute_max_memory_mb: int = 128
    cors_allowed_origins: list[str] = [
        "http://localhost:5173",
        "http://notebook.com",
        "https://notebook.com",
        "http://notebook.local",
        "http://localhost:3000",
    ]
    cors_allowed_origin_regex: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def normalized_app_env(self) -> str:
        """Return a normalized environment name."""
        return self.app_env.strip().lower()

    @property
    def is_local_like(self) -> bool:
        """Whether dev/local/test-only behavior is allowed."""
        return self.normalized_app_env in LOCAL_ENVS

    @property
    def is_production_like(self) -> bool:
        """Whether production-grade safety checks must be enforced."""
        return self.normalized_app_env in PRODUCTION_ENVS

    @property
    def placeholder_auth_enabled(self) -> bool:
        """Whether placeholder ``X-User-Id`` auth is enabled."""
        if self.allow_placeholder_auth is not None:
            return self.allow_placeholder_auth and self.is_local_like
        return self.is_local_like

    @model_validator(mode="after")
    def validate_auth_settings(self) -> "Settings":
        """Validate production-sensitive auth settings."""
        if self.jwt_access_ttl_seconds <= 0:
            raise ValueError("JWT_ACCESS_TTL_SECONDS must be positive")
        if self.jwt_refresh_ttl_seconds <= 0:
            raise ValueError("JWT_REFRESH_TTL_SECONDS must be positive")
        if self.otp_ttl_seconds <= 0:
            raise ValueError("OTP_TTL_SECONDS must be positive")
        if self.otp_max_attempts <= 0:
            raise ValueError("OTP_MAX_ATTEMPTS must be positive")
        if self.otp_rate_limit_per_email <= 0:
            raise ValueError("OTP_RATE_LIMIT_PER_EMAIL must be positive")
        if self.resend_request_timeout_seconds <= 0:
            raise ValueError("RESEND_REQUEST_TIMEOUT_SECONDS must be positive")
        if self.llm_request_timeout_seconds <= 0:
            raise ValueError("LLM_REQUEST_TIMEOUT_SECONDS must be positive")
        if self.llm_max_prompt_bytes <= 0:
            raise ValueError("LLM_MAX_PROMPT_BYTES must be positive")
        if self.llm_max_total_bytes <= 0:
            raise ValueError("LLM_MAX_TOTAL_BYTES must be positive")
        if self.llm_max_total_bytes < self.llm_max_prompt_bytes:
            raise ValueError("LLM_MAX_TOTAL_BYTES must be greater than or equal to LLM_MAX_PROMPT_BYTES")
        if self.llm_rate_limit_per_minute <= 0:
            raise ValueError("LLM_RATE_LIMIT_PER_MINUTE must be positive")
        if self.llm_validation_max_retries < 0:
            raise ValueError("LLM_VALIDATION_MAX_RETRIES must be non-negative")
        if self.llm_validation_max_retries > 2:
            raise ValueError(
                "LLM_VALIDATION_MAX_RETRIES must be <= 2 "
                "(docs/ai-architecture.md §7.1 caps total attempts at 3)"
            )
        if self.llm_validation_timeout_seconds <= 0:
            raise ValueError("LLM_VALIDATION_TIMEOUT_SECONDS must be positive")
        if self.llm_max_tokens <= 0:
            raise ValueError("LLM_MAX_TOKENS must be positive")
        if not 0 <= self.llm_temperature <= 2:
            raise ValueError("LLM_TEMPERATURE must be between 0 and 2")
        if self.execute_default_timeout_ms <= 0:
            raise ValueError("EXECUTE_DEFAULT_TIMEOUT_MS must be positive")
        if self.execute_max_timeout_ms <= 0:
            raise ValueError("EXECUTE_MAX_TIMEOUT_MS must be positive")
        if self.execute_max_timeout_ms < self.execute_default_timeout_ms:
            raise ValueError(
                "EXECUTE_MAX_TIMEOUT_MS must be greater than or equal to "
                "EXECUTE_DEFAULT_TIMEOUT_MS"
            )
        if self.execute_max_code_bytes <= 0:
            raise ValueError("EXECUTE_MAX_CODE_BYTES must be positive")
        if self.execute_max_output_bytes <= 0:
            raise ValueError("EXECUTE_MAX_OUTPUT_BYTES must be positive")
        if self.execute_max_memory_mb <= 0:
            raise ValueError("EXECUTE_MAX_MEMORY_MB must be positive")
        if self.llm_context_summary_strategy.strip() not in ALLOWED_SUMMARY_STRATEGIES:
            allowed = ", ".join(sorted(ALLOWED_SUMMARY_STRATEGIES))
            raise ValueError(
                f"LLM_CONTEXT_SUMMARY_STRATEGY must be one of: {allowed}"
            )

        if self.is_production_like:
            for field_name, value in [
                ("LLM_BEDROCK_GUARD_MODEL_ID", self.llm_bedrock_guard_model_id),
                (
                    "LLM_BEDROCK_GENERATOR_MODEL_ID",
                    self.llm_bedrock_generator_model_id,
                ),
            ]:
                if not value.startswith("eu."):
                    raise ValueError(
                        f"{field_name} must use an EU Geo inference profile "
                        "with the 'eu.' prefix in production-like environments"
                    )
            if self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < 32:
                raise ValueError(
                    "JWT_SECRET must be set to a non-default value of at least 32 characters in production-like environments"
                )
            if (
                self.otp_hash_secret == DEV_OTP_HASH_SECRET
                or len(self.otp_hash_secret) < 32
            ):
                raise ValueError(
                    "OTP_HASH_SECRET must be set to a non-default value of at least 32 characters in production-like environments"
                )
            if self.allow_placeholder_auth:
                raise ValueError(
                    "ALLOW_PLACEHOLDER_AUTH cannot be enabled in production-like environments"
                )
            if self.enable_execute:
                raise ValueError(
                    "ENABLE_EXECUTE cannot be enabled in production-like "
                    "environments until a hardened execution runtime exists"
                )
            if not self.resend_api_key:
                raise ValueError(
                    "RESEND_API_KEY must be set in production-like environments"
                )
            if self.email_from == DEV_EMAIL_FROM or not EMAIL_RE.match(self.email_from):
                raise ValueError(
                    "EMAIL_FROM must be set to a verified sender address "
                    "(not the default 'noreply@example.com') in "
                    "production-like environments"
                )
        return self


settings = Settings()
