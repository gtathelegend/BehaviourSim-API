"""Tests verifying OpenAPI schema invariants and API contract completeness."""

import pytest
from app.main import create_app


@pytest.fixture(scope="module")
def openapi_schema():
    """Generate OpenAPI schema dictionary from application factory."""
    app = create_app()
    return app.openapi()


def test_openapi_basic_metadata(openapi_schema):
    """Verify OpenAPI version and top-level application info."""
    assert openapi_schema["openapi"].startswith("3.")
    assert openapi_schema["info"]["title"] == "BehaviorSim API"
    assert "version" in openapi_schema["info"]


def test_openapi_all_expected_routes_present(openapi_schema):
    """Verify that all core contract routes are present in OpenAPI paths."""
    paths = openapi_schema["paths"]

    expected_routes = [
        "/health",
        "/ready",
        "/v1/auth/{provider}",
        "/v1/auth/{provider}/callback",
        "/v1/auth/logout",
        "/v1/account",
        "/v1/api-keys",
        "/v1/api-keys/{key_id}",
        "/v1/usage",
        "/v1/presets",
        "/v1/presets/{preset}",
        "/v1/simulations",
    ]

    for route in expected_routes:
        assert route in paths, f"Expected route '{route}' missing from OpenAPI paths: {list(paths.keys())}"


def test_openapi_security_schemes(openapi_schema):
    """Verify that HTTPBearer security scheme is defined."""
    components = openapi_schema.get("components", {})
    security_schemes = components.get("securitySchemes", {})
    assert "HTTPBearer" in security_schemes
    assert security_schemes["HTTPBearer"]["type"] == "http"
    assert security_schemes["HTTPBearer"]["scheme"] == "bearer"


def test_openapi_protected_routes_declare_security(openapi_schema):
    """Verify that authenticated routes declare security requirement."""
    paths = openapi_schema["paths"]

    protected_endpoints = [
        ("/v1/account", "get"),
        ("/v1/usage", "get"),
        ("/v1/api-keys", "get"),
        ("/v1/api-keys", "post"),
        ("/v1/api-keys/{key_id}", "delete"),
        ("/v1/simulations", "post"),
    ]

    for path, method in protected_endpoints:
        op = paths[path][method]
        assert "security" in op, f"Expected security on {method.upper()} {path}"
        assert any("HTTPBearer" in s for s in op["security"]), f"HTTPBearer expected on {method.upper()} {path}"


def test_openapi_public_routes_have_no_mandatory_security(openapi_schema):
    """Verify public discovery and health routes are accessible without bearer requirements."""
    paths = openapi_schema["paths"]
    public_endpoints = [
        ("/health", "get"),
        ("/ready", "get"),
        ("/v1/presets", "get"),
        ("/v1/presets/{preset}", "get"),
    ]

    for path, method in public_endpoints:
        op = paths[path][method]
        assert op.get("security") is None or len(op.get("security", [])) == 0


def test_openapi_schema_contains_no_sensitive_leaks(openapi_schema):
    """Verify that schema descriptions and parameters do not leak secrets or connection strings."""
    import json
    schema_text = json.dumps(openapi_schema)

    forbidden_keywords = [
        "postgresql://",
        "GOCSPX-",
        "secret-key",
        "password",
        "private_key",
    ]
    for keyword in forbidden_keywords:
        assert keyword not in schema_text, f"Potential sensitive leak detected: '{keyword}' in OpenAPI schema"
