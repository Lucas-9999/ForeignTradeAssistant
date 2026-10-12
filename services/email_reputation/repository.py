"""SQLite repository for Phase 1 email reputation records.

This module is persistence-only. Sending code does not import it until later
phases explicitly integrate suppression and the Sending Guard.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from email_validator import EmailNotValidError, validate_email

from services.email_reputation.diagnostic_safety import sanitize_diagnostic
from services.email_reputation.models import (
    DnsStatus,
    DomainHealth,
    HealthStatus,
    MailboxHealth,
    RecipientDomainMetric,
    ReputationEntityType,
    ReputationEvent,
    ReputationEventType,
    ReputationSeverity,
    RiskLevel,
    SuppressionReason,
    SuppressionRecord,
)


class EmailReputationRepository:
    """Public persistence seam for reputation and suppression data."""

    _SENSITIVE_KEY = re.compile(
        r"(?:^|_)(?:password|passwd|passphrase|secret|token|api_key|"
        r"credential|authorization)(?:$|_)",
        re.IGNORECASE,
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    @staticmethod
    def _normalize_email(email: str) -> str:
        try:
            validated = validate_email(
                email.strip(),
                check_deliverability=False,
                test_environment=True,
            )
        except (EmailNotValidError, AttributeError, TypeError) as exc:
            raise ValueError("email must contain a valid local part and domain") from exc
        return validated.normalized.lower()

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        candidate = domain.strip().rstrip(".")
        try:
            validated = validate_email(
                f"domain-check@{candidate}",
                check_deliverability=False,
                test_environment=True,
            )
        except (EmailNotValidError, AttributeError, TypeError) as exc:
            raise ValueError("domain must be a valid DNS name") from exc
        normalized = validated.domain.lower()
        if "." not in normalized:
            raise ValueError("domain must be a valid DNS name")
        return normalized

    @classmethod
    def _sanitize_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return sanitize_diagnostic(value)

    @classmethod
    def _sanitize_metadata_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, child in value.items():
                key_text = str(key)
                snake_key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key_text)
                normalized_key = re.sub(
                    r"[^a-z0-9]+", "_", snake_key.lower()
                ).strip("_")
                if cls._SENSITIVE_KEY.search(normalized_key):
                    raise ValueError(f"sensitive metadata key is not allowed: {key_text}")
                sanitized[key_text] = cls._sanitize_metadata_value(child)
            return sanitized
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_metadata_value(child) for child in value]
        if isinstance(value, str):
            return cls._sanitize_text(value)
        return value

    @classmethod
    def _serialize_metadata(cls, metadata: Optional[dict[str, Any]]) -> str:
        sanitized = cls._sanitize_metadata_value(metadata or {})
        return json.dumps(sanitized, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _validate_suppression_expiry(
        *, is_permanent: bool, expires_at: Optional[str]
    ) -> Optional[str]:
        if not is_permanent and not expires_at:
            raise ValueError("expires_at is required for a temporary suppression")
        if not expires_at:
            return None
        try:
            parsed = datetime.fromisoformat(expires_at.strip().replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("expires_at must be a valid ISO timestamp") from exc
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _decode_metadata(value: Optional[str]) -> dict[str, Any]:
        if not value:
            return {}
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}

    def _suppression_from_row(self, row: sqlite3.Row) -> SuppressionRecord:
        return SuppressionRecord(
            id=row["id"],
            email=row["email"],
            domain=row["domain"],
            reason=SuppressionReason(row["reason"]),
            source=row["source"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            is_permanent=bool(row["is_permanent"]),
            metadata=self._decode_metadata(row["metadata_json"]),
        )

    def _event_from_row(self, row: sqlite3.Row) -> ReputationEvent:
        return ReputationEvent(
            id=row["id"],
            entity_type=ReputationEntityType(row["entity_type"]),
            entity_id=row["entity_id"],
            event_type=ReputationEventType(row["event_type"]),
            severity=ReputationSeverity(row["severity"]),
            old_score=row["old_score"],
            new_score=row["new_score"],
            reason=row["reason"],
            metadata=self._decode_metadata(row["metadata_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _mailbox_health_from_row(row: sqlite3.Row) -> MailboxHealth:
        return MailboxHealth(
            mailbox=row["mailbox"],
            health_score=row["health_score"],
            status=HealthStatus(row["status"]),
            daily_sent=row["daily_sent"],
            hourly_sent=row["hourly_sent"],
            hard_bounce_rate=row["hard_bounce_rate"],
            soft_bounce_rate=row["soft_bounce_rate"],
            reply_rate=row["reply_rate"],
            complaint_rate=row["complaint_rate"],
            last_error=row["last_error"],
            last_health_check=row["last_health_check"],
            provider=row["provider"],
            sending_ip=row["sending_ip"],
            domain=row["domain"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _domain_health_from_row(row: sqlite3.Row) -> DomainHealth:
        return DomainHealth(
            domain=row["domain"],
            health_score=row["health_score"],
            status=HealthStatus(row["status"]),
            mx_status=DnsStatus(row["mx_status"]),
            spf_status=DnsStatus(row["spf_status"]),
            dkim_status=DnsStatus(row["dkim_status"]),
            dmarc_status=DnsStatus(row["dmarc_status"]),
            a_status=DnsStatus(row["a_status"]),
            ptr_status=DnsStatus(row["ptr_status"]),
            tls_status=DnsStatus(row["tls_status"]),
            sending_ip=row["sending_ip"],
            authentication_status=DnsStatus(row["authentication_status"]),
            last_checked=row["last_checked"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _recipient_metric_from_row(row: sqlite3.Row) -> RecipientDomainMetric:
        return RecipientDomainMetric(
            id=row["id"],
            domain=row["domain"],
            metric_date=row["metric_date"],
            metric_hour=row["metric_hour"],
            mx_provider=row["mx_provider"],
            hourly_sent=row["hourly_sent"],
            daily_sent=row["daily_sent"],
            bounce_rate=row["bounce_rate"],
            deferral_rate=row["deferral_rate"],
            last_smtp_error=row["last_smtp_error"],
            risk_level=RiskLevel(row["risk_level"]),
            updated_at=row["updated_at"],
        )

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
        """Create one normalized email suppression and return its record."""
        expires_at = self._validate_suppression_expiry(
            is_permanent=is_permanent, expires_at=expires_at
        )
        normalized = self._normalize_email(email)
        domain = normalized.rsplit("@", 1)[1]
        values = (
            reason.value,
            source.strip() or "system",
            expires_at,
            int(is_permanent),
            self._serialize_metadata(metadata),
        )
        self.conn.execute(
            """INSERT INTO email_suppressions
               (email, domain, reason, source, expires_at, is_permanent, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO UPDATE SET
                   reason = excluded.reason,
                   source = excluded.source,
                   expires_at = excluded.expires_at,
                   is_permanent = excluded.is_permanent,
                   metadata_json = excluded.metadata_json""",
            (normalized, domain, *values),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM email_suppressions WHERE LOWER(email) = ?", (normalized,)
        ).fetchone()
        if row is None:
            raise RuntimeError("suppression record disappeared after write")
        return self._suppression_from_row(row)

    def find_active_suppression(self, email: str) -> Optional[SuppressionRecord]:
        """Find an active email or domain suppression for a recipient."""
        normalized = self._normalize_email(email)
        domain = normalized.rsplit("@", 1)[1]
        row = self.conn.execute(
            """SELECT * FROM email_suppressions
               WHERE (LOWER(email) = ? OR (email IS NULL AND LOWER(domain) = ?))
                 AND (is_permanent = 1 OR expires_at > CURRENT_TIMESTAMP)
               ORDER BY CASE WHEN email IS NOT NULL THEN 0 ELSE 1 END, id DESC
               LIMIT 1""",
            (normalized, domain),
        ).fetchone()
        return self._suppression_from_row(row) if row else None

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
        """Create or replace one canonical domain-level suppression."""
        expires_at = self._validate_suppression_expiry(
            is_permanent=is_permanent, expires_at=expires_at
        )
        normalized = self._normalize_domain(domain)
        values = (
            reason.value,
            source.strip() or "system",
            expires_at,
            int(is_permanent),
            self._serialize_metadata(metadata),
        )
        self.conn.execute(
            """INSERT INTO email_suppressions
               (email, domain, reason, source, expires_at, is_permanent, metadata_json)
               VALUES (NULL, ?, ?, ?, ?, ?, ?)
               ON CONFLICT DO UPDATE SET
                   reason = excluded.reason,
                   source = excluded.source,
                   expires_at = excluded.expires_at,
                   is_permanent = excluded.is_permanent,
                   metadata_json = excluded.metadata_json""",
            (normalized, *values),
        )
        self.conn.commit()
        row = self.conn.execute(
            """SELECT * FROM email_suppressions
               WHERE email IS NULL AND LOWER(domain) = ?""",
            (normalized,),
        ).fetchone()
        if row is None:
            raise RuntimeError("suppression record disappeared after write")
        return self._suppression_from_row(row)

    def append_reputation_event(
        self,
        *,
        entity_type: ReputationEntityType,
        entity_id: str,
        event_type: ReputationEventType,
        severity: ReputationSeverity,
        reason: str,
        old_score: Optional[float] = None,
        new_score: Optional[float] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> ReputationEvent:
        """Append an immutable reputation event."""
        cursor = self.conn.execute(
            """INSERT INTO reputation_events
               (entity_type, entity_id, event_type, severity, old_score,
                new_score, reason, metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity_type.value,
                entity_id.strip(),
                event_type.value,
                severity.value,
                old_score,
                new_score,
                self._sanitize_text(reason.strip()),
                self._serialize_metadata(metadata),
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM reputation_events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        if row is None:
            raise RuntimeError("reputation event disappeared after append")
        return self._event_from_row(row)

    def list_reputation_events(
        self,
        *,
        entity_type: ReputationEntityType,
        entity_id: str,
        limit: int = 100,
    ) -> list[ReputationEvent]:
        """Return newest reputation events for one entity."""
        rows = self.conn.execute(
            """SELECT * FROM reputation_events
               WHERE entity_type = ? AND entity_id = ?
               ORDER BY id DESC LIMIT ?""",
            (entity_type.value, entity_id, max(1, min(limit, 1000))),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def upsert_mailbox_health(self, health: MailboxHealth) -> MailboxHealth:
        """Store the latest health snapshot for one configured mailbox."""
        self.conn.execute(
            """INSERT INTO mailbox_health
               (mailbox, health_score, status, daily_sent, hourly_sent,
                hard_bounce_rate, soft_bounce_rate, reply_rate, complaint_rate,
                last_error, last_health_check, provider, sending_ip, domain)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(mailbox) DO UPDATE SET
                   health_score = excluded.health_score,
                   status = excluded.status,
                   daily_sent = excluded.daily_sent,
                   hourly_sent = excluded.hourly_sent,
                   hard_bounce_rate = excluded.hard_bounce_rate,
                   soft_bounce_rate = excluded.soft_bounce_rate,
                   reply_rate = excluded.reply_rate,
                   complaint_rate = excluded.complaint_rate,
                   last_error = excluded.last_error,
                   last_health_check = excluded.last_health_check,
                   provider = excluded.provider,
                   sending_ip = excluded.sending_ip,
                   domain = excluded.domain,
                   updated_at = CURRENT_TIMESTAMP""",
            (
                health.mailbox,
                health.health_score,
                health.status.value,
                health.daily_sent,
                health.hourly_sent,
                health.hard_bounce_rate,
                health.soft_bounce_rate,
                health.reply_rate,
                health.complaint_rate,
                self._sanitize_text(health.last_error),
                health.last_health_check,
                health.provider,
                health.sending_ip,
                self._normalize_domain(health.domain),
            ),
        )
        self.conn.commit()
        found = self.get_mailbox_health(health.mailbox)
        if found is None:
            raise RuntimeError("mailbox health disappeared after upsert")
        return found

    def get_mailbox_health(self, mailbox: str) -> Optional[MailboxHealth]:
        """Read the current health snapshot for one mailbox."""
        row = self.conn.execute(
            "SELECT * FROM mailbox_health WHERE mailbox = ?", (mailbox,)
        ).fetchone()
        return self._mailbox_health_from_row(row) if row else None

    def upsert_domain_health(self, health: DomainHealth) -> DomainHealth:
        """Store the latest DNS/authentication health snapshot for a domain."""
        domain = self._normalize_domain(health.domain)
        self.conn.execute(
            """INSERT INTO domain_health
               (domain, health_score, status, mx_status, spf_status, dkim_status,
                dmarc_status, a_status, ptr_status, tls_status, sending_ip,
                authentication_status, last_checked)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(domain) DO UPDATE SET
                   health_score = excluded.health_score,
                   status = excluded.status,
                   mx_status = excluded.mx_status,
                   spf_status = excluded.spf_status,
                   dkim_status = excluded.dkim_status,
                   dmarc_status = excluded.dmarc_status,
                   a_status = excluded.a_status,
                   ptr_status = excluded.ptr_status,
                   tls_status = excluded.tls_status,
                   sending_ip = excluded.sending_ip,
                   authentication_status = excluded.authentication_status,
                   last_checked = excluded.last_checked,
                   updated_at = CURRENT_TIMESTAMP""",
            (
                domain,
                health.health_score,
                health.status.value,
                health.mx_status.value,
                health.spf_status.value,
                health.dkim_status.value,
                health.dmarc_status.value,
                health.a_status.value,
                health.ptr_status.value,
                health.tls_status.value,
                health.sending_ip,
                health.authentication_status.value,
                health.last_checked,
            ),
        )
        self.conn.commit()
        found = self.get_domain_health(domain)
        if found is None:
            raise RuntimeError("domain health disappeared after upsert")
        return found

    def get_domain_health(self, domain: str) -> Optional[DomainHealth]:
        """Read the current health snapshot for one domain."""
        normalized = self._normalize_domain(domain)
        row = self.conn.execute(
            "SELECT * FROM domain_health WHERE domain = ?", (normalized,)
        ).fetchone()
        return self._domain_health_from_row(row) if row else None

    def list_domain_health(self, *, limit: int = 100) -> list[DomainHealth]:
        """Return newest persisted domain-health snapshots, without auditing."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        rows = self.conn.execute(
            """SELECT * FROM domain_health
               ORDER BY last_checked DESC, domain ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [self._domain_health_from_row(row) for row in rows]

    def upsert_recipient_domain_metric(
        self, metric: RecipientDomainMetric
    ) -> RecipientDomainMetric:
        """Store one recipient-domain hourly snapshot."""
        domain = self._normalize_domain(metric.domain)
        self.conn.execute(
            """INSERT INTO recipient_domain_metrics
               (domain, metric_date, metric_hour, mx_provider, hourly_sent,
                daily_sent, bounce_rate, deferral_rate, last_smtp_error, risk_level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(domain, metric_date, metric_hour) DO UPDATE SET
                   mx_provider = excluded.mx_provider,
                   hourly_sent = excluded.hourly_sent,
                   daily_sent = excluded.daily_sent,
                   bounce_rate = excluded.bounce_rate,
                   deferral_rate = excluded.deferral_rate,
                   last_smtp_error = excluded.last_smtp_error,
                   risk_level = excluded.risk_level,
                   updated_at = CURRENT_TIMESTAMP""",
            (
                domain,
                metric.metric_date,
                metric.metric_hour,
                metric.mx_provider,
                metric.hourly_sent,
                metric.daily_sent,
                metric.bounce_rate,
                metric.deferral_rate,
                self._sanitize_text(metric.last_smtp_error),
                metric.risk_level.value,
            ),
        )
        self.conn.commit()
        found = self.get_recipient_domain_metric(
            domain, metric.metric_date, metric.metric_hour
        )
        if found is None:
            raise RuntimeError("recipient-domain metric disappeared after upsert")
        return found

    def get_recipient_domain_metric(
        self, domain: str, metric_date: str, metric_hour: int
    ) -> Optional[RecipientDomainMetric]:
        """Read one recipient-domain hourly snapshot."""
        normalized = self._normalize_domain(domain)
        row = self.conn.execute(
            """SELECT * FROM recipient_domain_metrics
               WHERE domain = ? AND metric_date = ? AND metric_hour = ?""",
            (normalized, metric_date, metric_hour),
        ).fetchone()
        return self._recipient_metric_from_row(row) if row else None
