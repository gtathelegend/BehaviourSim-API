"""Application-level error handling foundation."""

from typing import Any, Dict, Optional
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class BehaviorSimAPIError(Exception):
    """Base exception class for all BehaviorSim API domain errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def register_error_handlers(app: FastAPI) -> None:
    """Register application-level exception handlers."""

    @app.exception_handler(BehaviorSimAPIError)
    async def handle_behaviorsim_api_error(
        request: Request, exc: BehaviorSimAPIError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": exc.message,
                    "status_code": exc.status_code,
                    "details": exc.details,
                }
            },
        )
