"""Tests for application importability and health endpoint."""

from app.core.config import get_settings
from app.main import app


def test_app_import():
    """Verify application imports cleanly."""
    assert app is not None
    assert app.title == get_settings().APP_NAME


def test_health_endpoint(client):
    """Verify /health returns HTTP 200 and matches expected structure."""
    response = client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert "status" in data
    assert data["status"] == "ok"
    assert "version" in data
    assert data["version"] == get_settings().API_VERSION
