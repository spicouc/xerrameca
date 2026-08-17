from __future__ import annotations

from typing import Literal

from pydantic import SecretStr
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
    XERRAMECA_IDENTITY_PROVIDER: Literal["unavailable", "pluribus"] = "unavailable"
    XERRAMECA_SUMMARY_DISPATCH_SECONDS: float = 30.0
    XERRAMECA_SUMMARY_MAX_ATTEMPTS: int = 10
    PLURIBUS_BASE_URL: str = "http://127.0.0.1:8790"
    PLURIBUS_TIMEOUT_SECONDS: float = 10.0
    # Optional integration-agent credential used only for deliberate Brain writes.
    # SecretStr prevents accidental plaintext representation; it is never persisted.
    PLURIBUS_SERVICE_API_KEY: SecretStr | None = None


settings = Settings()
