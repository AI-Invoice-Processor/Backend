from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI Invoice Processor API"
    environment: str = "development"
    database_url: str = (
        "postgresql://invoice:local-dev-password-change-me@db:5432/invoices"
    )
    allowed_origins: str = "http://localhost:5173,http://localhost"
    max_upload_size: int = 10 * 1024 * 1024
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"
    documents_dir: Path = Path("data/documents")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.allowed_origins.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
