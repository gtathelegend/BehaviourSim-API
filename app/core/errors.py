"""Application-level error handling foundation."""

import logging
from typing import Any, Dict, Optional
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.middleware import get_current_request_id

logger = logging.getLogger("behaviorsim_api.core.errors")


class BehaviorSimAPIError(Exception):
    """Base exception class for all BehaviorSim API domain errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        details: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.headers = headers


def register_error_handlers(app: FastAPI) -> None:
    """Register application-level exception handlers."""

    @app.exception_handler(BehaviorSimAPIError)
    async def handle_behaviorsim_api_error(
        request: Request, exc: BehaviorSimAPIError
    ) -> JSONResponse:
        req_id = get_current_request_id() or getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": exc.message,
                    "status_code": exc.status_code,
                    "details": exc.details,
                    "request_id": req_id,
                }
            },
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        req_id = get_current_request_id() or getattr(request.state, "request_id", None)
        sanitized_errors = []
        for err in exc.errors():
            sanitized_errors.append({
                "loc": [str(loc) for loc in err.get("loc", [])],
                "msg": err.get("msg", "Invalid parameter"),
                "type": err.get("type", "value_error"),
            })
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "message": "Invalid request parameters.",
                    "status_code": 422,
                    "details": {
                        "code": "validation_error",
                        "errors": sanitized_errors,
                    },
                    "request_id": req_id,
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unhandled_exception(
        request: Request, exc: Exception
    ) -> JSONResponse:
        req_id = get_current_request_id() or getattr(request.state, "request_id", None)
        logger.error(
            "Unhandled server exception on %s %s [req:%s]: %s",
            request.method,
            request.url.path,
            req_id,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "An unexpected server error occurred. Please try again or contact support.",
                    "status_code": 500,
                    "details": {
                        "code": "internal_server_error",
                    },
                    "request_id": req_id,
                }
            },
        )
