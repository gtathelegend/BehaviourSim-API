"""Tests verifying application-level error handling foundation."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import BehaviorSimAPIError, register_error_handlers


def test_custom_api_error_handler():
    """Verify BehaviorSimAPIError produces structured error responses."""
    test_app = FastAPI()
    register_error_handlers(test_app)

    @test_app.get("/trigger-error")
    def trigger_error():
        raise BehaviorSimAPIError(
            message="Test custom exception",
            status_code=400,
            details={"field": "test_param"},
        )

    with TestClient(test_app) as client:
        response = client.get("/trigger-error")
        assert response.status_code == 400
        data = response.json()
        assert "error" in data
        assert data["error"]["message"] == "Test custom exception"
        assert data["error"]["status_code"] == 400
        assert data["error"]["details"] == {"field": "test_param"}
