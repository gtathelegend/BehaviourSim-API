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
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5432/behaviorsim_dev"

    # Authentication & API Key configuration
    API_KEY_PREFIX: str = "bs_live_"
    SESSION_TOKEN_PREFIX: str = "bs_sess_"

    # Session & Cookie configuration
    AUTH_SESSION_COOKIE_NAME: str = "behaviorsim_session"
    AUTH_SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 7  # 7 days
    AUTH_OAUTH_STATE_COOKIE_NAME: str = "behaviorsim_oauth_state"
    AUTH_OAUTH_STATE_MAX_AGE_SECONDS: int = 600  # 10 minutes

    # OAuth configuration (Google & GitHub)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    OAUTH_REDIRECT_BASE_URL: str = "http://localhost:8000"

    # CORS configuration
    CORS_ORIGINS: Union[List[str], str] = [
        "http://localhost:3000",
        "http://localhost:8000",
    ]

    # Database connection pool settings (for external RDBMS)
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE: int = 1800

    # Logging
    LOG_LEVEL: str = "INFO"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Normalize database URL for SQLAlchemy 2 with psycopg3 if postgres:// or postgresql:// is provided."""
        if isinstance(value, str):
            if value.startswith("postgres://"):
                return value.replace("postgres://", "postgresql+psycopg://", 1)
            if value.startswith("postgresql://"):
                return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

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

    def validate_production_configuration(self) -> None:
        """Validate production prerequisites. Fails fast during startup if misconfigured."""
        if not self.is_production:
            return

        errors: List[str] = []
        db_lower = self.DATABASE_URL.lower()
        if "sqlite" in db_lower or "localhost" in db_lower or "127.0.0.1" in db_lower:
            errors.append("DATABASE_URL must point to an external production database (not sqlite or localhost)")

        if not self.API_BASE_URL.startswith("https://"):
            errors.append("API_BASE_URL must use HTTPS in production")

        if not self.WEB_BASE_URL.startswith("https://"):
            errors.append("WEB_BASE_URL must use HTTPS in production")

        if not self.OAUTH_REDIRECT_BASE_URL.startswith("https://"):
            errors.append("OAUTH_REDIRECT_BASE_URL must use HTTPS in production")

        # Reject default placeholder OAuth credentials in production
        for field_name, val in [
            ("GOOGLE_CLIENT_ID", self.GOOGLE_CLIENT_ID),
            ("GOOGLE_CLIENT_SECRET", self.GOOGLE_CLIENT_SECRET),
            ("GITHUB_CLIENT_ID", self.GITHUB_CLIENT_ID),
            ("GITHUB_CLIENT_SECRET", self.GITHUB_CLIENT_SECRET),
        ]:
            if val and ("your-" in val.lower() or "client-secret" in val.lower() or "example" in val.lower()):
                errors.append(f"{field_name} contains an insecure placeholder value")

        for origin in self.CORS_ORIGINS:
            if origin == "*" or "localhost" in origin.lower() or "127.0.0.1" in origin:
                errors.append(f"CORS origin '{origin}' is unsafe for production")

        if self.DB_POOL_SIZE <= 0:
            errors.append("DB_POOL_SIZE must be greater than 0")

        if self.DB_MAX_OVERFLOW < 0:
            errors.append("DB_MAX_OVERFLOW cannot be negative")

        if self.DB_POOL_RECYCLE <= 0:
            errors.append("DB_POOL_RECYCLE must be greater than 0")

        if self.AUTH_SESSION_MAX_AGE_SECONDS <= 0:
            errors.append("AUTH_SESSION_MAX_AGE_SECONDS must be positive")

        if errors:
            raise ValueError(f"Production configuration validation failed: {'; '.join(errors)}")


@lru_cache()
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()
