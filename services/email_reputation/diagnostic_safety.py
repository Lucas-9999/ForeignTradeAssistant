"""Secret and recipient redaction for persisted or logged diagnostics."""

from __future__ import annotations

import re
from typing import Any, Optional


_AUTHORIZATION_CREDENTIAL = re.compile(
    r"\bauthorization\s*([:=])\s*(?:basic|bearer)\s+[^\s,;]+",
    re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_QUOTED_SECRET_ASSIGNMENT = re.compile(
    r'''(["']?(?:password|passwd|passphrase|api[_-]?key|access[_-]?token|'''
    r'''refresh[_-]?token|token|secret|authorization|credential)["']?'''
    r'''\s*:\s*)["']([^"']+)["']''',
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(password|passwd|passphrase|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|token|secret|authorization|credential)\b"
    r"(\s*[:=]\s*)([^\s,;]+)",
    re.IGNORECASE,
)
_EMAIL_ADDRESS = re.compile(
    r'''(?:"(?:[^"\\]|\\.)+"|[^\s<>()\[\],;:@"']+)@'''
    r'''(?:\[[^\]\s]+\]|[^\s<>()\[\],;:@"']+)''',
    re.IGNORECASE | re.UNICODE,
)


def sanitize_diagnostic(
    value: Any, *, max_length: Optional[int] = 2000
) -> str:
    """Return compact diagnostic text with credentials and addresses removed."""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    text = " ".join(text.split())
    text = _AUTHORIZATION_CREDENTIAL.sub(
        lambda match: f"Authorization{match.group(1)} [REDACTED]", text
    )
    text = _BEARER_TOKEN.sub("Bearer [REDACTED]", text)
    text = _QUOTED_SECRET_ASSIGNMENT.sub(
        lambda match: f'{match.group(1)}"[REDACTED]"', text
    )
    text = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text
    )
    text = _EMAIL_ADDRESS.sub("[EMAIL_REDACTED]", text)
    if max_length is None:
        return text
    return text[: max(0, max_length)]
