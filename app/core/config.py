from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "MSD FastAPI Template"
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

    jwt_secret_key: str = "dev-only-insecure-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
