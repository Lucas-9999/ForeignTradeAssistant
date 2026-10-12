"""Pure bounce classification with no persistence or sending side effects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from services.email_reputation.smtp_error_parser import (
    ParsedSmtpError,
    SmtpDiagnosticEvent,
    SmtpDiagnosticSignal,
)


class BounceCategory(str, Enum):
    HARD = "hard"
    SOFT = "soft"
    REPUTATION = "reputation"
    RATE_LIMIT = "rate_limit"
    AUTHENTICATION = "authentication"
    POLICY = "policy"
    CONTENT = "content"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BounceClassification:
    category: BounceCategory
    is_retryable: bool
    should_suppress_recipient: bool
    reason: str


def classify_bounce(
    error: ParsedSmtpError | SmtpDiagnosticEvent,
) -> BounceClassification:
    """Classify one parsed SMTP/DSN error without applying an action."""
    if (
        error.smtp_code in {530, 535}
        or error.enhanced_status_code == "5.7.8"
        or SmtpDiagnosticSignal.AUTHENTICATION in error.signals
    ):
        return BounceClassification(
            category=BounceCategory.AUTHENTICATION,
            is_retryable=False,
            should_suppress_recipient=False,
            reason="sender authentication requires remediation",
        )
    if error.enhanced_status_code in {
        "5.1.1",
        "5.1.2",
        "5.1.3",
        "5.1.4",
        "5.1.6",
        "5.1.10",
    }:
        return BounceClassification(
            category=BounceCategory.HARD,
            is_retryable=False,
            should_suppress_recipient=True,
            reason="recipient address is permanently invalid",
        )
    if error.enhanced_status_code == "5.2.2":
        return BounceClassification(
            category=BounceCategory.SOFT,
            is_retryable=True,
            should_suppress_recipient=False,
            reason="recipient mailbox is temporarily full",
        )
    if SmtpDiagnosticSignal.REPUTATION in error.signals:
        return BounceClassification(
            category=BounceCategory.REPUTATION,
            is_retryable=False,
            should_suppress_recipient=False,
            reason="sender or domain reputation caused rejection",
        )
    if SmtpDiagnosticSignal.RATE_LIMIT in error.signals:
        return BounceClassification(
            category=BounceCategory.RATE_LIMIT,
            is_retryable=True,
            should_suppress_recipient=False,
            reason="provider rate limit requires delayed retry",
        )
    if SmtpDiagnosticSignal.CONTENT in error.signals:
        return BounceClassification(
            category=BounceCategory.CONTENT,
            is_retryable=False,
            should_suppress_recipient=False,
            reason="message content caused rejection",
        )
    if (
        (error.enhanced_status_code or "").startswith("5.7.")
        or SmtpDiagnosticSignal.POLICY in error.signals
    ):
        return BounceClassification(
            category=BounceCategory.POLICY,
            is_retryable=False,
            should_suppress_recipient=False,
            reason="provider policy caused rejection",
        )
    if (
        (error.enhanced_status_code or "").startswith("4.")
        or error.smtp_code in {421, 450, 451, 452}
        or SmtpDiagnosticSignal.TEMPORARY in error.signals
    ):
        return BounceClassification(
            category=BounceCategory.SOFT,
            is_retryable=True,
            should_suppress_recipient=False,
            reason="delivery failure is temporary",
        )
    if (
        SmtpDiagnosticSignal.INVALID_RECIPIENT in error.signals
        or SmtpDiagnosticSignal.INVALID_DESTINATION_DOMAIN in error.signals
    ):
        return BounceClassification(
            category=BounceCategory.HARD,
            is_retryable=False,
            should_suppress_recipient=True,
            reason="recipient address is permanently invalid",
        )
    return BounceClassification(
        category=BounceCategory.UNKNOWN,
        is_retryable=False,
        should_suppress_recipient=False,
        reason="diagnostic did not match a known bounce category",
    )
