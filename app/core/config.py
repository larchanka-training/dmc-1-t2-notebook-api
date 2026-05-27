"""Centralised application settings.

Все переменные окружения, флаги фичей и параметры подключения собраны
в одном месте — в классе :class:`Settings`. Pydantic читает их из
``.env`` (или из реального окружения), валидирует типы и кэширует в
синглтоне :data:`settings`, который импортируют все модули.

Конвенция: значение по умолчанию = безопасный dev-default. Любая
prod-ценная вещь (DB URL, OAuth-секрет, JSON-логи) должна быть
перекрыта переменной окружения при деплое.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
