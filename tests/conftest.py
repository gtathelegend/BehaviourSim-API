"""Shared pytest fixtures for BehaviorSim API tests."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    """Create a TestClient instance for API tests."""
    with TestClient(app) as test_client:
        yield test_client
