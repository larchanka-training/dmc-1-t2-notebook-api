"""Centralised application settings.

Все переменные окружения, флаги фичей и параметры подключения собраны
в одном месте — в классе :class:`Settings`. Pydantic читает их из
``.env`` (или из реального окружения), валидирует типы и кэширует в
синглтоне :data:`settings`, который импортируют все модули.

Конвенция: значение по умолчанию = безопасный dev-default. Любая
prod-ценная вещь (DB URL, OAuth-секрет, JSON-логи) должна быть
перекрыта переменной окружения при деплое.
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-only-jwt-secret-change-me-32-bytes-minimum"
LOCAL_ENVS = {"dev", "local", "test"}
PRODUCTION_ENVS = {"production", "prod", "staging"}


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
    app_version: str = "0.1.0"
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
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 2_592_000
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_rate_limit_per_email: int = 3
    allow_placeholder_auth: bool | None = None
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

        if self.is_production_like:
            if self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < 32:
                raise ValueError(
                    "JWT_SECRET must be set to a non-default value of at least 32 characters in production-like environments"
                )
            if self.allow_placeholder_auth:
                raise ValueError(
                    "ALLOW_PLACEHOLDER_AUTH cannot be enabled in production-like environments"
                )
        return self


settings = Settings()
