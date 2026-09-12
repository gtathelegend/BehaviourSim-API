"""Application configuration management using Pydantic Settings."""

from functools import lru_cache
from typing import List, Union
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized environment-based settings for BehaviorSim API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # Core application information
    APP_ENV: str = "development"
    APP_NAME: str = "BehaviorSim API"
    API_VERSION: str = "0.1.0"

    # Base URLs
    API_BASE_URL: str = "http://localhost:8000"
    WEB_BASE_URL: str = "http://localhost:3000"

    # Database configuration (Phase 1+)
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/behaviorsim_dev"

    # CORS configuration
    CORS_ORIGINS: Union[List[str], str] = [
        "http://localhost:3000",
        "http://localhost:8000",
    ]

    # Logging
    LOG_LEVEL: str = "INFO"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Union[List[str], str]) -> List[str]:
        """Parse comma-separated strings or validate list of origins."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, list):
            return [str(origin).strip() for origin in value if str(origin).strip()]
        return []

    @property
    def is_production(self) -> bool:
        """Helper to determine if running in production mode."""
        return self.APP_ENV.lower() == "production"


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
