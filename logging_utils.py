"""Logging helpers that keep credentials out of application logs."""
from __future__ import annotations

import logging
import re


_TELEGRAM_URL_TOKEN = re.compile(
    r"(?i)(https?://api\.telegram\.org/bot)[^/\s]+"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|token|access_token|secret)=)[^&\s]+"
)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]+")


def redact_secrets(value: str) -> str:
    """Mask common credentials that can appear inside logged request URLs."""
    value = _TELEGRAM_URL_TOKEN.sub(r"\1[REDACTED]", value)
    value = _QUERY_SECRET.sub(r"\1[REDACTED]", value)
    return _OPENAI_KEY.sub("[REDACTED]", value)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        record.msg = redact_secrets(rendered)
        record.args = ()
        return True


def install_secret_redaction() -> None:
    """Install redaction on every handler configured on the root logger."""
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretRedactionFilter())
