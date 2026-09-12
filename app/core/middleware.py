"""Operational and security middlewares for request correlation and security headers."""

import contextvars
import re
import uuid
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

# Context variable for request correlation ID
request_id_ctx_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# Safe pattern for incoming correlation IDs (alphanumeric, hyphens, underscores, dots; 1-64 chars)
SAFE_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-\.]{1,64}$")


def get_current_request_id() -> str:
    """Retrieve the current request correlation ID from context."""
    return request_id_ctx_var.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Middleware to extract or generate and propagate X-Request-ID correlation headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming_id = request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-ID")
        if incoming_id and SAFE_REQUEST_ID_PATTERN.match(incoming_id):
            request_id = incoming_id
        else:
            request_id = str(uuid.uuid4())

        token = request_id_ctx_var.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_ctx_var.reset(token)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing standard HTTP security headers on all API responses."""

    def __init__(self, app, is_production: bool = False) -> None:
        super().__init__(app)
        self.is_production = is_production

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none';"
        if self.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
