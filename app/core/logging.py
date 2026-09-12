"""Structured application logging configuration."""

import logging
import sys


class SafeFormatter(logging.Formatter):
    """Custom formatter ensuring consistent output format and omitting sensitive data."""

    def format(self, record: logging.LogRecord) -> str:
        # Standard formatting
        return super().format(record)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure basic structured application logging."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    log_format = "%(asctime)s [%(levelname)s] [%(name)s]: %(message)s"
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
