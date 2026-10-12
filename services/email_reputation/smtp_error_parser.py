"""Pure SMTP/DSN diagnostic parsing for email reputation Phase 2."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from email_validator import EmailNotValidError, validate_email

from services.email_reputation.diagnostic_safety import sanitize_diagnostic


_SMTP_CODE = re.compile(r"(?<![\d.])([245]\d{2})(?![\d.])")
_ENHANCED_STATUS_CODE = re.compile(
    r"(?<![\d.])([245]\.\d{1,3}\.\d{1,3})(?![\d.])"
)


class SmtpDiagnosticSignal(str, Enum):
    AUTHENTICATION = "authentication"
    REPUTATION = "reputation"
    RATE_LIMIT = "rate_limit"
    CONTENT = "content"
    POLICY = "policy"
    TEMPORARY = "temporary"
    INVALID_RECIPIENT = "invalid_recipient"
    INVALID_DESTINATION_DOMAIN = "invalid_destination_domain"


_SIGNAL_PATTERNS: dict[SmtpDiagnosticSignal, tuple[str, ...]] = {
    SmtpDiagnosticSignal.AUTHENTICATION: (
        "authentication failed",
        "authentication credentials invalid",
        "authentication required",
        "spf fail",
        "dkim fail",
        "dmarc fail",
    ),
    SmtpDiagnosticSignal.REPUTATION: (
        "ip reputation",
        "domain reputation",
        "sender reputation",
        "poor reputation",
        "low reputation",
        "reputation of the sending",
        "blacklisted",
        "blocklisted",
        "spamhaus",
    ),
    SmtpDiagnosticSignal.RATE_LIMIT: (
        "rate limit",
        "rate-limit",
        "too many messages",
        "too many emails",
        "sending rate",
        "throttled",
        "throttling",
    ),
    SmtpDiagnosticSignal.CONTENT: (
        "message content",
        "content rejected",
        "content filter",
        "prohibited attachment",
        "spam content",
        "spam detected",
    ),
    SmtpDiagnosticSignal.POLICY: (
        "relay denied",
        "policy rejection",
        "policy violation",
        "not permitted",
        "access denied",
        "administrative prohibition",
    ),
    SmtpDiagnosticSignal.TEMPORARY: (
        "mailbox full",
        "temporary failure",
        "greylisting",
    ),
    SmtpDiagnosticSignal.INVALID_RECIPIENT: (
        "user unknown",
        "user not local",
        "no such user",
        "mailbox does not exist",
        "mailbox not found",
        "invalid recipient",
        "invalid mailbox",
    ),
    SmtpDiagnosticSignal.INVALID_DESTINATION_DOMAIN: (
        "recipient domain does not exist",
        "destination domain does not exist",
        "no such recipient domain",
        "recipient domain not found",
        "domain of the recipient address does not exist",
    ),
}


def _extract_signals(message: str) -> frozenset[SmtpDiagnosticSignal]:
    lowered = message.lower()
    return frozenset(
        signal
        for signal, patterns in _SIGNAL_PATTERNS.items()
        if any(pattern in lowered for pattern in patterns)
    )


@dataclass(frozen=True)
class ParsedSmtpError:
    """Structured, side-effect-free representation of one SMTP diagnostic."""

    smtp_code: Optional[int]
    enhanced_status_code: Optional[str]
    provider: Optional[str]
    recipient_domain: Optional[str]
    message: str
    observed_at: Optional[str]
    signals: frozenset[SmtpDiagnosticSignal]


@dataclass(frozen=True)
class SmtpDiagnosticEvent:
    """Complete context envelope for a future persistence/handler seam."""

    smtp_code: Optional[int]
    enhanced_status_code: Optional[str]
    provider: str
    recipient_domain: str
    message: str
    observed_at: str
    signals: frozenset[SmtpDiagnosticSignal]


def _normalize_provider(provider: Optional[str]) -> Optional[str]:
    if not provider or not provider.strip():
        return None
    normalized = provider.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", normalized):
        raise ValueError("provider must be a safe identifier")
    return normalized


def _normalize_recipient_domain(domain: Optional[str]) -> Optional[str]:
    if not domain or not domain.strip():
        return None
    candidate = domain.strip().rstrip(".")
    try:
        validated = validate_email(
            f"domain-check@{candidate}",
            check_deliverability=False,
            test_environment=True,
        )
    except (EmailNotValidError, AttributeError, TypeError) as exc:
        raise ValueError("recipient_domain must be a valid DNS name") from exc
    normalized = validated.domain.lower()
    if "." not in normalized:
        raise ValueError("recipient_domain must be a valid DNS name")
    return normalized


def _normalize_observed_at(value: Optional[str]) -> Optional[str]:
    if not value or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("observed_at must be a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_smtp_error(
    message: object,
    *,
    provider: Optional[str] = None,
    recipient_domain: Optional[str] = None,
    observed_at: Optional[str] = None,
) -> ParsedSmtpError:
    """Extract protocol codes and normalized context without taking action."""
    complete_message = sanitize_diagnostic(message, max_length=None)
    smtp_match = _SMTP_CODE.search(complete_message)
    enhanced_match = _ENHANCED_STATUS_CODE.search(complete_message)
    return ParsedSmtpError(
        smtp_code=int(smtp_match.group(1)) if smtp_match else None,
        enhanced_status_code=enhanced_match.group(1) if enhanced_match else None,
        provider=_normalize_provider(provider),
        recipient_domain=_normalize_recipient_domain(recipient_domain),
        message=complete_message[:2000],
        observed_at=_normalize_observed_at(observed_at),
        signals=_extract_signals(complete_message),
    )


def build_smtp_diagnostic_event(
    message: object,
    *,
    provider: str,
    recipient_domain: str,
    observed_at: str,
) -> SmtpDiagnosticEvent:
    """Build the complete, validated envelope required by event handlers."""
    parsed = parse_smtp_error(
        message,
        provider=provider,
        recipient_domain=recipient_domain,
        observed_at=observed_at,
    )
    if not parsed.provider or not parsed.recipient_domain or not parsed.observed_at:
        raise ValueError("provider, recipient_domain, and observed_at are required")
    return SmtpDiagnosticEvent(
        smtp_code=parsed.smtp_code,
        enhanced_status_code=parsed.enhanced_status_code,
        provider=parsed.provider,
        recipient_domain=parsed.recipient_domain,
        message=parsed.message,
        observed_at=parsed.observed_at,
        signals=parsed.signals,
    )
