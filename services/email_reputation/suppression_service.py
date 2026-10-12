"""Unified suppression decisions for every outbound email path."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from services.email_reputation.bounce_classifier import classify_bounce
from services.email_reputation.models import SuppressionReason, SuppressionRecord
from services.email_reputation.repository import EmailReputationRepository
from services.email_reputation.smtp_error_parser import build_smtp_diagnostic_event


class SuppressionScope(str, Enum):
    EMAIL = "email"
    DOMAIN = "domain"


class SuppressionPersistenceError(RuntimeError):
    """A confirmed hard bounce could not reach canonical persistence."""


@dataclass(frozen=True)
class SuppressionDecision:
    blocked: bool
    reason: Optional[SuppressionReason] = None
    source: Optional[str] = None
    scope: Optional[SuppressionScope] = None
    is_legacy: bool = False


class SuppressionService:
    """Read and write the canonical suppression policy for one database."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.repository = EmailReputationRepository(conn)

    def suppress_email(
        self,
        email: str,
        *,
        reason: SuppressionReason,
        source: str,
        expires_at: Optional[str] = None,
        is_permanent: bool = True,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SuppressionRecord:
        return self.repository.suppress_email(
            email,
            reason=reason,
            source=source,
            expires_at=expires_at,
            is_permanent=is_permanent,
            metadata=metadata,
        )

    def suppress_domain(
        self,
        domain: str,
        *,
        reason: SuppressionReason,
        source: str,
        expires_at: Optional[str] = None,
        is_permanent: bool = True,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SuppressionRecord:
        return self.repository.suppress_domain(
            domain,
            reason=reason,
            source=source,
            expires_at=expires_at,
            is_permanent=is_permanent,
            metadata=metadata,
        )

    def suppress_from_smtp_error(
        self,
        email: str,
        diagnostic: object,
        *,
        provider: str,
    ) -> Optional[SuppressionRecord]:
        recipient_domain = email.strip().rsplit("@", 1)[-1]
        event = build_smtp_diagnostic_event(
            diagnostic,
            provider=provider,
            recipient_domain=recipient_domain,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )
        classification = classify_bounce(event)
        if not classification.should_suppress_recipient:
            return None
        source = f"smtp:{event.provider}"
        try:
            return self.suppress_email(
                email,
                reason=SuppressionReason.HARD_BOUNCE,
                source=source,
                metadata={
                    "smtp_code": event.smtp_code,
                    "enhanced_status_code": event.enhanced_status_code,
                    "category": classification.category.value,
                    "provider": event.provider,
                    "recipient_domain": event.recipient_domain,
                    "observed_at": event.observed_at,
                },
            )
        except sqlite3.Error as exc:
            raise SuppressionPersistenceError(
                "confirmed hard-bounce suppression persistence failed"
            ) from exc

    def check(self, email: str) -> SuppressionDecision:
        try:
            record = self.repository.find_active_suppression(email)
        except ValueError:
            return SuppressionDecision(
                blocked=True,
                reason=SuppressionReason.INVALID_EMAIL,
                source="validation",
                scope=SuppressionScope.EMAIL,
            )
        if record is not None:
            scope = (
                SuppressionScope.EMAIL
                if record.email is not None
                else SuppressionScope.DOMAIN
            )
            return SuppressionDecision(
                blocked=True,
                reason=record.reason,
                source=record.source,
                scope=scope,
            )

        legacy = self.conn.execute(
            "SELECT reason FROM blacklist WHERE LOWER(email) = LOWER(?) LIMIT 1",
            (email.strip(),),
        ).fetchone()
        if legacy is None:
            return SuppressionDecision(blocked=False)
        return SuppressionDecision(
            blocked=True,
            reason=self._legacy_reason(legacy["reason"]),
            source="legacy_blacklist",
            scope=SuppressionScope.EMAIL,
            is_legacy=True,
        )

    @staticmethod
    def _legacy_reason(value: Optional[str]) -> SuppressionReason:
        normalized = (value or "").strip().lower()
        if "unsub" in normalized:
            return SuppressionReason.UNSUBSCRIBE
        if "complaint" in normalized or "spam" in normalized:
            return SuppressionReason.SPAM_COMPLAINT
        if "bounce" in normalized:
            return SuppressionReason.HARD_BOUNCE
        if "invalid" in normalized:
            return SuppressionReason.INVALID_EMAIL
        if "dead" in normalized and "domain" in normalized:
            return SuppressionReason.DEAD_DOMAIN
        if "do not contact" in normalized or "do_not_contact" in normalized:
            return SuppressionReason.DO_NOT_CONTACT
        return SuppressionReason.MANUAL_BLOCK
