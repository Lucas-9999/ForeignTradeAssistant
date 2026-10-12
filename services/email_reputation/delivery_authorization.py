"""P11 one-time authorization persistence. This module never sends mail."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional
from uuid import uuid4

from email_validator import EmailNotValidError, validate_email

logger = logging.getLogger(__name__)


class Purpose(str, Enum):
    CONTROLLED_TEST = "controlled_test"
    MANUAL_VERIFICATION = "manual_verification"


class AuthorizationStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"


class AuthorizationDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class DeliveryAuthorization:
    """One exact scope with an auditable lifecycle record."""

    id: int
    lead_id: int
    email: str
    mailbox: str
    purpose: Purpose
    expires_at: str
    status: AuthorizationStatus
    delivery_attempt_id: Optional[str]
    created_by: str
    created_at: str
    approval_note: str
    closed_at: Optional[str]
    closed_by: Optional[str]


@dataclass(frozen=True)
class AuthorizationAttempt:
    """Metadata-only result of one authorization decision."""

    decision: AuthorizationDecision
    authorization_id: Optional[int]
    attempt_id: str
    reason: str


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class DeliveryAuthorizationRepository:
    """Deep persistence module for exact, expiring, one-time authorizations."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row

    @staticmethod
    def normalize_email(value: str) -> str:
        """Validate one recipient and return its canonical lower-case address."""
        try:
            validated = validate_email(value.strip(), check_deliverability=False, test_environment=True)
        except (EmailNotValidError, AttributeError, TypeError) as exc:
            raise ValueError("email must contain a valid local part and domain") from exc
        normalized = validated.normalized.lower()
        if "." not in normalized.rsplit("@", 1)[1]:
            raise ValueError("email must contain a valid local part and domain")
        return normalized

    @staticmethod
    def _row(row: sqlite3.Row) -> DeliveryAuthorization:
        return DeliveryAuthorization(
            id=row["id"], lead_id=row["lead_id"], email=row["email"], mailbox=row["mailbox"],
            purpose=Purpose(row["purpose"]), expires_at=row["expires_at"],
            status=AuthorizationStatus(row["status"]), delivery_attempt_id=row["delivery_attempt_id"],
            created_by=row["created_by"], created_at=row["created_at"],
            approval_note=row["approval_note"], closed_at=row["closed_at"], closed_by=row["closed_by"],
        )

    def create(self, *, lead_id: int, email: str, mailbox: str, purpose: Purpose,
               expires_at: datetime, created_by: str, approval_note: str,
               now: datetime) -> DeliveryAuthorization:
        email = self.normalize_email(email)
        mailbox, created_by, approval_note = mailbox.strip(), created_by.strip(), approval_note.strip()
        if not mailbox or not created_by or not approval_note:
            raise ValueError("mailbox, created_by, and approval_note are required")
        now_text, expiry_text = _stamp(now), _stamp(expires_at)
        if expiry_text <= now_text:
            raise ValueError("expires_at must be in the future")
        # IMMEDIATE makes expiry closure + partial-unique insert one writer turn.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """UPDATE delivery_authorizations SET status='expired', closed_at=?, closed_by='system'
                   WHERE lead_id=? AND email=? AND mailbox=? AND purpose=?
                     AND status='active' AND expires_at <= ?""",
                (now_text, lead_id, email, mailbox, purpose.value, now_text),
            )
            active = self.conn.execute(
                """SELECT 1 FROM delivery_authorizations WHERE lead_id=? AND email=?
                   AND mailbox=? AND purpose=? AND status='active' LIMIT 1""",
                (lead_id, email, mailbox, purpose.value),
            ).fetchone()
            if active:
                raise ValueError("active authorization already exists for this scope")
            cur = self.conn.execute(
                """INSERT INTO delivery_authorizations
                   (lead_id,email,mailbox,purpose,expires_at,created_by,created_at,approval_note)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (lead_id, email, mailbox, purpose.value, expiry_text, created_by, now_text, approval_note),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get(cur.lastrowid)

    def get(self, authorization_id: int) -> DeliveryAuthorization:
        row = self.conn.execute("SELECT * FROM delivery_authorizations WHERE id=?", (authorization_id,)).fetchone()
        if row is None:
            raise LookupError("delivery authorization not found")
        return self._row(row)

    def close_expired(self, *, now: datetime) -> int:
        """Close elapsed active records without changing their approved scope."""
        cur = self.conn.execute(
            """UPDATE delivery_authorizations SET status='expired', closed_at=?, closed_by='system'
               WHERE status='active' AND expires_at <= ?""",
            (_stamp(now), _stamp(now)),
        )
        self.conn.commit()
        return cur.rowcount

    def list(self, *, limit: int = 50, status: Optional[AuthorizationStatus] = None,
             now: Optional[datetime] = None) -> list[DeliveryAuthorization]:
        """Close elapsed records, then list at most 100 latest authorization records."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self.close_expired(now=now or datetime.now(timezone.utc))
        sql, values = "SELECT * FROM delivery_authorizations", []
        if status is not None:
            sql += " WHERE status=?"
            values.append(status.value)
        rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*values, limit)).fetchall()
        return [self._row(row) for row in rows]

    def find_active(self, *, lead_id: int, email: str, mailbox: str, purpose: Purpose,
                    now: datetime) -> Optional[DeliveryAuthorization]:
        self.close_expired(now=now)
        row = self.conn.execute(
            """SELECT * FROM delivery_authorizations WHERE lead_id=? AND email=? AND mailbox=?
               AND purpose=? AND status='active' AND expires_at > ? LIMIT 1""",
            (lead_id, self.normalize_email(email), mailbox, purpose.value, _stamp(now)),
        ).fetchone()
        return self._row(row) if row else None

    def consume(self, authorization_id: int, attempt_id: str, *, now: datetime) -> bool:
        cur = self.conn.execute(
            """UPDATE delivery_authorizations SET status='consumed', closed_at=?, delivery_attempt_id=?
               WHERE id=? AND status='active' AND expires_at > ?""",
            (_stamp(now), attempt_id, authorization_id, _stamp(now)),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def revoke(self, authorization_id: int, *, revoked_by: str, now: datetime) -> bool:
        """Close an active authorization without deleting its audit record."""
        if not revoked_by.strip():
            raise ValueError("revoked_by is required")
        cur = self.conn.execute(
            """UPDATE delivery_authorizations SET status='revoked', closed_at=?, closed_by=?
               WHERE id=? AND status='active'""",
            (_stamp(now), revoked_by.strip(), authorization_id),
        )
        self.conn.commit()
        return cur.rowcount == 1


class DeliveryAuthorizationGate:
    """Authorize and consume exact P11 scope without any transport dependency."""

    def __init__(self, repository: DeliveryAuthorizationRepository, *, now: Callable[[], datetime]):
        self.repository = repository
        self._now = now

    def _record(self, *, decision: AuthorizationDecision, authorization_id: Optional[int],
                attempt_id: str, reason: str, lead_id: Optional[int], email: str,
                mailbox: str, purpose: Optional[Purpose]) -> AuthorizationAttempt:
        logger.info("delivery_authorization_decision %s", json.dumps({
            "decision": decision.value, "authorization_id": authorization_id,
            "attempt_id": attempt_id, "reason": reason, "lead_id": lead_id,
            "email": email, "mailbox": mailbox,
            "purpose": purpose.value if purpose else None,
        }, ensure_ascii=False, sort_keys=True))
        return AuthorizationAttempt(decision, authorization_id, attempt_id, reason)

    def authorize_and_consume(self, *, lead_id: Optional[int], email: str,
                              mailbox: str, purpose: Optional[Purpose]) -> AuthorizationAttempt:
        """Fail closed and audit every attempt; ALLOW records a one-time CAS consume."""
        attempt_id = uuid4().hex
        if lead_id is None:
            return self._record(decision=AuthorizationDecision.DENY, authorization_id=None,
                attempt_id=attempt_id, reason="missing_lead_id", lead_id=lead_id, email=email,
                mailbox=mailbox, purpose=purpose)
        if purpose is None:
            return self._record(decision=AuthorizationDecision.DENY, authorization_id=None,
                attempt_id=attempt_id, reason="missing_purpose", lead_id=lead_id, email=email,
                mailbox=mailbox, purpose=purpose)
        try:
            found = self.repository.find_active(lead_id=lead_id, email=email, mailbox=mailbox,
                purpose=purpose, now=self._now())
        except ValueError:
            return self._record(decision=AuthorizationDecision.DENY, authorization_id=None,
                attempt_id=attempt_id, reason="invalid_email", lead_id=lead_id, email=email,
                mailbox=mailbox, purpose=purpose)
        if found is None:
            return self._record(decision=AuthorizationDecision.DENY, authorization_id=None,
                attempt_id=attempt_id, reason="not_found_or_expired", lead_id=lead_id, email=email,
                mailbox=mailbox, purpose=purpose)
        allowed = self.repository.consume(found.id, attempt_id, now=self._now())
        return self._record(
            decision=AuthorizationDecision.ALLOW if allowed else AuthorizationDecision.DENY,
            authorization_id=found.id, attempt_id=attempt_id,
            reason="authorized_and_consumed" if allowed else "consume_raced",
            lead_id=lead_id, email=email, mailbox=mailbox, purpose=purpose,
        )
