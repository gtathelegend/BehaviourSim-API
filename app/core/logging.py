"""Structured application logging configuration with automatic secret redaction and context correlation."""

import logging
import re
import sys
from typing import Optional

from app.core.middleware import get_current_request_id

# Regex compilation for redacting sensitive secrets from all log lines
REDACTION_PATTERNS = [
    # Authorization header / Bearer token
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.]+"), "Bearer [REDACTED]"),
    # Live API key
    (re.compile(r"\bbs_live_[A-Za-z0-9_]{16,}\b"), "bs_live_[REDACTED]"),
    # Session token
    (re.compile(r"\bbs_sess_[A-Za-z0-9_]{16,}\b"), "bs_sess_[REDACTED]"),
    # Session cookie
    (re.compile(r"(?i)behaviorsim_session=[A-Za-z0-9_\-\.]+"), "behaviorsim_session=[REDACTED]"),
    # Key-value secrets (passwords, tokens, client_secrets)
    (
        re.compile(r"(?i)\b(password|client_secret|access_token|refresh_token|api_key|secret)\s*[:=]\s*([^\s,;'\"]+)"),
        r"\1=[REDACTED]",
    ),
    # Database connection URLs with credentials (e.g. postgresql://user:pass@host)
    (
        re.compile(r"(?i)(postgres(?:ql)?(?:\+[a-z0-9]+)?://[^:\s]+:)([^@\s]+)(@)"),
        r"\1[REDACTED]\3",
    ),
]


def redact_sensitive_text(text: str) -> str:
    """Scrub sensitive credentials, tokens, API keys, and connection strings from text."""
    if not text:
        return text
    sanitized = text
    for pattern, replacement in REDACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


class SafeFormatter(logging.Formatter):
    """Custom formatter ensuring consistent output format, correlation ID injection, and omitting sensitive data."""

    def format(self, record: logging.LogRecord) -> str:
        # 1. Resolve request correlation ID from contextvars or record attribute
        req_id = getattr(record, "request_id", None) or get_current_request_id()
        sim_id = getattr(record, "simulation_id", None)
        user_id = getattr(record, "user_id", None)

        tags = []
        if req_id:
            tags.append(f"req:{req_id}")
        if sim_id:
            tags.append(f"sim:{sim_id}")
        if user_id:
            tags.append(f"user:{user_id}")

        record.request_context = f" [{' '.join(tags)}]" if tags else ""

        # 2. Format record message using standard logging formatter
        formatted = super().format(record)

        # 3. Apply comprehensive secret scrubbing on final output string
        return redact_sensitive_text(formatted)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure basic structured application logging."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    log_format = "%(asctime)s [%(levelname)s] [%(name)s]%(request_context)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    formatter = SafeFormatter(fmt=log_format, datefmt=date_format)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Avoid duplicate handlers if setup_logging is called multiple times
    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
        root_logger.addHandler(handler)
    else:
        root_logger.handlers = [handler]

    # Silence overly verbose third-party loggers if needed
    logging.getLogger("uvicorn.access").setLevel(numeric_level)
    logging.getLogger("uvicorn.error").setLevel(numeric_level)

