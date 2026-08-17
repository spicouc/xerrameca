from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
    )

    XERRAMECA_HOST: str = "0.0.0.0"
    XERRAMECA_PORT: int = 8791
    XERRAMECA_DB_PATH: str = "/var/lib/xerrameca/xerrameca.db"
    PLURIBUS_BASE_URL: str = "http://127.0.0.1:8790"
    PLURIBUS_TIMEOUT_SECONDS: float = 10.0


settings = Settings()
