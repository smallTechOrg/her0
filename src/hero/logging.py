"""structlog JSON-lines logging to stdout. Every line carries ts, level, event, request_id."""

from __future__ import annotations

import logging
import re
import sys

import structlog

_CONFIGURED = False

_KEY_RE = re.compile(r"(?i)([?&\s\"']key=)[^&\s\"'>)]+")
_NOISY_LOGGERS = ("httpx", "httpcore")


def redact_keys(text: str) -> str:
    """Replace the value of every ``key=`` query parameter with ``REDACTED``."""
    return _KEY_RE.sub(r"\1REDACTED", text)


class RedactKeyFilter(logging.Filter):
    """Scrubs ``key=<secret>`` query values from the message (and args) of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if "key=" in msg.lower():
            record.msg = redact_keys(msg)
            record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(stream=sys.stdout, level=numeric, format="%(message)s", force=True)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactKeyFilter) for f in handler.filters):
            handler.addFilter(RedactKeyFilter())
    for name in _NOISY_LOGGERS:  # httpx logs full request URLs (with ?key=) at INFO
        logging.getLogger(name).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()


def bind_request(request_id: str) -> None:
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)


def unbind_request() -> None:
    structlog.contextvars.clear_contextvars()
