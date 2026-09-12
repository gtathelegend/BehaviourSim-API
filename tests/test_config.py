"""Tests verifying application configuration behavior."""

import os
from unittest.mock import patch

from app.core.config import Settings


def test_default_settings():
    """Verify default local configuration is valid without requiring external services or secrets."""
    settings = Settings()
    assert settings.APP_ENV == "development"
    assert settings.APP_NAME == "BehaviorSim API"
    assert settings.API_VERSION == "0.1.0"
    assert "http://localhost:8000" in settings.API_BASE_URL
    assert isinstance(settings.CORS_ORIGINS, list)
    assert not settings.is_production


def test_settings_environment_override():
    """Verify environment variables properly override default settings."""
    env_overrides = {
        "APP_ENV": "production",
        "APP_NAME": "BehaviorSim Production API",
        "API_VERSION": "1.0.0",
        "CORS_ORIGINS": "https://behaviorsim.com,https://app.behaviorsim.com",
    }
    with patch.dict(os.environ, env_overrides, clear=False):
        settings = Settings()
        assert settings.APP_ENV == "production"
        assert settings.APP_NAME == "BehaviorSim Production API"
        assert settings.API_VERSION == "1.0.0"
        assert settings.is_production is True
        assert settings.CORS_ORIGINS == [
            "https://behaviorsim.com",
            "https://app.behaviorsim.com",
        ]


def test_cors_origins_parsing():
    """Verify CORS origins string is correctly parsed into a list."""
    settings = Settings(CORS_ORIGINS="http://example.com, http://test.com")
    assert settings.CORS_ORIGINS == ["http://example.com", "http://test.com"]
