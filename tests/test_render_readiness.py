"""Tests verifying Render deployment readiness, blueprint integrity, and configuration normalization."""

import os
from pathlib import Path
import pytest
import yaml

from app.core.config import Settings


def test_python_version_file_matches_supported_runtime():
    """Verify .python-version file exists and specifies an exact supported CPython version."""
    py_version_path = Path(__file__).resolve().parent.parent / ".python-version"
    assert py_version_path.exists(), ".python-version file must exist in repository root"
    content = py_version_path.read_text().strip()
    assert content == "3.12.10"


def test_database_url_normalization_postgres_and_postgresql():
    """Verify that both postgres:// and postgresql:// are normalized to postgresql+psycopg://, preserving query params."""
    s1 = Settings(DATABASE_URL="postgres://usr:pwd@host.render.com:5432/mydb")
    assert s1.DATABASE_URL == "postgresql+psycopg://usr:pwd@host.render.com:5432/mydb"

    s2 = Settings(DATABASE_URL="postgresql://usr:pwd@host.render.com:5432/mydb")
    assert s2.DATABASE_URL == "postgresql+psycopg://usr:pwd@host.render.com:5432/mydb"

    s3 = Settings(DATABASE_URL="postgresql+psycopg://usr:pwd@host.render.com:5432/mydb")
    assert s3.DATABASE_URL == "postgresql+psycopg://usr:pwd@host.render.com:5432/mydb"

    # Verify Neon SSL query parameter preservation
    neon_url = "postgresql://user:pass@ep-plain-snow-123456.us-east-2.aws.neon.tech/neondb?sslmode=require"
    s_neon = Settings(DATABASE_URL=neon_url)
    assert s_neon.DATABASE_URL == "postgresql+psycopg://user:pass@ep-plain-snow-123456.us-east-2.aws.neon.tech/neondb?sslmode=require"


def test_render_blueprint_validity_and_safety():
    """Verify render.yaml structure, services, and zero-secret invariants for Neon PostgreSQL architecture."""
    blueprint_path = Path(__file__).resolve().parent.parent / "render.yaml"
    assert blueprint_path.exists(), "render.yaml must exist in repository root"

    with open(blueprint_path, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)

    # 1. Databases section must NOT be present (PostgreSQL is hosted on Neon, not Render)
    assert "databases" not in spec or spec.get("databases") is None

    # 2. Services section
    assert "services" in spec
    web_services = [s for s in spec["services"] if s.get("type") == "web"]
    assert len(web_services) == 1, "Must contain exactly one web service"
    svc_spec = web_services[0]
    assert svc_spec["type"] == "web"
    assert svc_spec["name"] == "behaviorsim-api"
    assert svc_spec["runtime"] == "python"
    assert svc_spec["buildCommand"] == "pip install -e ."
    assert svc_spec["preDeployCommand"] == "alembic upgrade head"
    assert "$PORT" in svc_spec["startCommand"]
    assert svc_spec["healthCheckPath"] == "/health"

    # 3. Verify environment variables and zero hardcoded secrets
    env_vars = {item["key"]: item for item in svc_spec["envVars"]}
    assert env_vars["APP_ENV"]["value"] == "production"
    assert env_vars["API_BASE_URL"]["value"] == "https://api.behaviorsim.vedaangsharma.in"
    assert env_vars["WEB_BASE_URL"]["value"] == "https://behaviorsim.vedaangsharma.in"
    assert env_vars["CORS_ORIGINS"]["value"] == "https://behaviorsim.vedaangsharma.in"

    # Database URL is supplied externally from Neon via Render secret dashboard (sync: false)
    assert "DATABASE_URL" in env_vars
    assert env_vars["DATABASE_URL"].get("sync") is False
    assert "value" not in env_vars["DATABASE_URL"], "DATABASE_URL must not contain hardcoded value in render.yaml"
    assert "fromDatabase" not in env_vars["DATABASE_URL"], "DATABASE_URL must not reference internal Render PostgreSQL"

    # Sensitive OAuth credentials must be marked sync: false
    for secret_key in ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"]:
        assert secret_key in env_vars
        assert env_vars[secret_key].get("sync") is False
        assert "value" not in env_vars[secret_key], f"Secret {secret_key} must not have hardcoded value in render.yaml"


def test_production_config_rejects_insecure_settings():
    """Verify Settings.validate_production_configuration enforces all production security rules."""
    # 1. Insecure database URL (sqlite or localhost)
    with pytest.raises(ValueError, match="DATABASE_URL must point to an external production database"):
        s = Settings(
            APP_ENV="production",
            DATABASE_URL="sqlite:///test.db",
            API_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
            WEB_BASE_URL="https://behaviorsim.vedaangsharma.in",
            OAUTH_REDIRECT_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
            CORS_ORIGINS=["https://behaviorsim.vedaangsharma.in"],
        )
        s.validate_production_configuration()

    # 2. Insecure CORS (wildcard or localhost)
    with pytest.raises(ValueError, match="CORS origin.*is unsafe for production"):
        s = Settings(
            APP_ENV="production",
            DATABASE_URL="postgresql+psycopg://user:pass@render-db:5432/mydb",
            API_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
            WEB_BASE_URL="https://behaviorsim.vedaangsharma.in",
            OAUTH_REDIRECT_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
            CORS_ORIGINS=["*"],
        )
        s.validate_production_configuration()

    # 3. Insecure URL protocol (HTTP instead of HTTPS)
    with pytest.raises(ValueError, match="API_BASE_URL must use HTTPS in production"):
        s = Settings(
            APP_ENV="production",
            DATABASE_URL="postgresql+psycopg://user:pass@render-db:5432/mydb",
            API_BASE_URL="http://api.behaviorsim.vedaangsharma.in",
            WEB_BASE_URL="https://behaviorsim.vedaangsharma.in",
            OAUTH_REDIRECT_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
            CORS_ORIGINS=["https://behaviorsim.vedaangsharma.in"],
        )
        s.validate_production_configuration()

    # 4. Insecure placeholder OAuth credentials
    with pytest.raises(ValueError, match="contains an insecure placeholder value"):
        s = Settings(
            APP_ENV="production",
            DATABASE_URL="postgresql+psycopg://user:pass@render-db:5432/mydb",
            API_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
            WEB_BASE_URL="https://behaviorsim.vedaangsharma.in",
            OAUTH_REDIRECT_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
            CORS_ORIGINS=["https://behaviorsim.vedaangsharma.in"],
            GOOGLE_CLIENT_ID="your-google-client-id.apps.googleusercontent.com",
        )
        s.validate_production_configuration()

    # 5. Valid production configuration passes validation
    valid_settings = Settings(
        APP_ENV="production",
        DATABASE_URL="postgresql+psycopg://prod_user:strong_password@dpg-host.oregon-postgres.render.com:5432/behaviorsim",
        API_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
        WEB_BASE_URL="https://behaviorsim.vedaangsharma.in",
        OAUTH_REDIRECT_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
        CORS_ORIGINS=["https://behaviorsim.vedaangsharma.in"],
        GOOGLE_CLIENT_ID="123456789.apps.googleusercontent.com",
        GOOGLE_CLIENT_SECRET="GOCSPX-real-secret-123456",
        GITHUB_CLIENT_ID="Iv1.real-client-id",
        GITHUB_CLIENT_SECRET="github_real_secret_123456",
    )
    valid_settings.validate_production_configuration()
