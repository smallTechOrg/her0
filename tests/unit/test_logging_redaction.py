"""httpx/httpcore are quiet at INFO and any ``key=`` query value is redacted from log records."""

from __future__ import annotations

import io
import logging

from hero.logging import RedactKeyFilter, configure_logging, redact_keys

SECRET = "AIzaSyFAKEKEY123"


def test_redact_keys_scrubs_query_values():
    out = redact_keys(f"GET https://maps.googleapis.com/x?center=1,2&key={SECRET}&zoom=19 ok")
    assert SECRET not in out
    assert "key=REDACTED" in out and "zoom=19" in out


def test_filter_redacts_message_and_args():
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "HTTP Request: GET %s", (f"https://g/x?key={SECRET}",), None)
    assert RedactKeyFilter().filter(rec)
    assert SECRET not in rec.getMessage()


def test_configure_logging_silences_httpx_info_and_redacts():
    configure_logging("INFO")
    try:
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.addFilter(RedactKeyFilter())
        lg = logging.getLogger("hero.test_redact")
        lg.addHandler(handler)
        lg.warning("url https://g/x?key=%s", SECRET)
        lg.removeHandler(handler)
        assert SECRET not in buf.getvalue()
        httpx_log = logging.getLogger("httpx")
        assert not httpx_log.isEnabledFor(logging.INFO)
    finally:
        logging.getLogger().handlers.clear()
