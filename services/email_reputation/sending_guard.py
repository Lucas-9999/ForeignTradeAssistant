"""SendingGuard — the single, typed, auditable pre-send decision engine.

Every real outbound send funnels through this guard before the SMTP transport
is reached. The guard makes decisions only: it holds no SMTP credentials, never
sends mail, and produces serializable, sanitized decisions from stable input
identifiers (recipient, mailbox, campaign, stage, requested time).

Decision model:
  ALLOW          — proceed to the transport now.
  DELAY          — do not send now; a ``retry_after`` timestamp is recommended.
  BLOCK          — do not send; the recipient/domain cannot be served.
  PAUSE_MAILBOX  — do not send anything through this mailbox until it recovers.

The guard composes the existing suppression policy and the existing
quota / MX / per-domain tier logic (via ``AntiSpamGuard.pre_send_verdict``)
instead of re-implementing them, and reads the mailbox-health seam only to
recognize statuses an operator already set (paused / restricted / disabled) —
it never invents limits or scores from an empty health table.

Authorized-scope note: persistent guard-decision storage (a GUARD_DECISION
event/table) is a schema change and is deliberately out of P4 scope. Decisions
are auditable through the injectable :class:`GuardDecisionRecorder`, whose
default is a structured, sanitized application log.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from services.email_reputation.models import HealthStatus, SuppressionReason
from services.email_reputation.suppression_service import SuppressionScope


class GuardAction(str, Enum):
    ALLOW = "ALLOW"
    DELAY = "DELAY"
    BLOCK = "BLOCK"
    PAUSE_MAILBOX = "PAUSE_MAILBOX"


class GuardReasonCode(str, Enum):
    ALLOW = "allow"
    DAILY_QUOTA_OVERRIDDEN = "daily_quota_overridden"
    INVALID_EMAIL = "invalid_email"
    SUPPRESSED_EMAIL = "suppressed_email"
    SUPPRESSED_DOMAIN = "suppressed_domain"
    LEGACY_BLACKLIST = "legacy_blacklist"
    DAILY_QUOTA_EXCEEDED = "daily_quota_exceeded"
    DOMAIN_QUOTA_EXCEEDED = "domain_quota_exceeded"
    NO_MX_RECORD = "no_mx_record"
    MAILBOX_PAUSED = "mailbox_paused"
    MAILBOX_DISABLED = "mailbox_disabled"
    MAILBOX_RESTRICTED = "mailbox_restricted"


@dataclass(frozen=True)
class GuardContext:
    """Stable, secret-free inputs a send decision is based on.

    ``override_daily_quota`` is an explicit, operator-approved bypass of the
    daily-quota DELAY only (e.g. a protected one-off tool invoked with ``--force``).
    It is recorded with every decision so the audit trail shows the override.
    All other guard rules (suppression, MX, per-domain tier, health status)
    still apply when it is set.
    """

    email: str
    mailbox: str = "default"
    lead_id: Optional[int] = None
    customer_id: Optional[int] = None
    campaign: Optional[str] = None
    stage: Optional[int] = None
    source: Optional[str] = None
    requested_time: Optional[str] = None
    override_daily_quota: bool = False


@dataclass(frozen=True)
class GuardDecision:
    """A deterministic, serializable send decision.

    ``message`` is a static, sanitized operator string — it never contains SMTP
    diagnostics, suppression source/provider provenance, secrets, or raw
    internal metadata.
    """

    action: GuardAction
    reason_code: GuardReasonCode
    message: str
    risk_score: float = 0.0
    retry_after: Optional[str] = None
    mailbox: str = "default"
    email: str = ""
    scope: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return self.action is not GuardAction.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason_code": self.reason_code.value,
            "message": self.message,
            "risk_score": self.risk_score,
            "retry_after": self.retry_after,
            "mailbox": self.mailbox,
            "scope": self.scope,
        }


class GuardDecisionRecorder(Protocol):
    """Injected sink for every evaluated decision."""

    def record(self, context: GuardContext, decision: GuardDecision) -> None: ...


class StructuredLogGuardRecorder:
    """Default recorder — one sanitized JSON line per decision to the app log.

    Not a database audit trail: persisting guard decisions requires a schema
    change (a GUARD_DECISION event type), which is out of P4 scope until
    explicitly authorized. This stream is structured and greppable so operators
    can audit decisions without exposing SMTP diagnostics, secrets, or
    suppression provenance in the output.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self._logger = logger or logging.getLogger("email_reputation.sending_guard")

    def record(self, context: GuardContext, decision: GuardDecision) -> None:
        self._logger.info(
            "guard_decision %s",
            json.dumps(
                {
                    "email": (context.email or "").strip() or None,
                    "mailbox": context.mailbox,
                    "lead_id": context.lead_id,
                    "customer_id": context.customer_id,
                    "campaign": context.campaign,
                    "stage": context.stage,
                    "source": context.source,
                    "requested_time": context.requested_time,
                    "override_daily_quota": context.override_daily_quota,
                    "action": decision.action.value,
                    "reason_code": decision.reason_code.value,
                    "risk_score": decision.risk_score,
                    "retry_after": decision.retry_after,
                    "scope": decision.scope,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )


class SendingGuard:
    """Compose suppression, the health-status seam, and the existing
    quota / MX / per-domain limits into one deterministic decision."""

    def __init__(
        self,
        *,
        suppression_service: Any,
        anti_spam_guard: Any,
        mailbox_health_reader: Optional[Callable[[str], Any]] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ):
        self._suppression = suppression_service
        self._anti_spam = anti_spam_guard
        self._mailbox_health_reader = mailbox_health_reader
        self._now_provider = now_provider or (
            lambda: datetime.now(timezone.utc)
        )

    def evaluate(self, context: GuardContext) -> GuardDecision:
        """Decide for one send request. No SMTP, no side effects beyond the
        recorder's sink, deterministic given the same DB state and clock."""
        now = self._now_provider()
        email = (context.email or "").strip()
        mailbox = context.mailbox or "default"

        if not email:
            return self._decision(
                GuardAction.BLOCK,
                GuardReasonCode.INVALID_EMAIL,
                "recipient address is empty",
                0.9,
                None,
                mailbox,
                "",
                "email",
            )

        # 1. Suppression policy (canonical + legacy) — highest priority, and
        #    fail-closed if the policy store is unavailable.
        try:
            suppression = self._suppression.check(email)
        except Exception:
            return self._decision(
                GuardAction.BLOCK,
                GuardReasonCode.SUPPRESSED_EMAIL,
                "unable to verify recipient suppression status",
                1.0,
                None,
                mailbox,
                email,
                "email",
            )
        if suppression.blocked:
            if suppression.reason is SuppressionReason.INVALID_EMAIL:
                return self._decision(
                    GuardAction.BLOCK,
                    GuardReasonCode.INVALID_EMAIL,
                    "recipient address is invalid",
                    0.9,
                    None,
                    mailbox,
                    email,
                    "email",
                )
            if suppression.scope is SuppressionScope.DOMAIN:
                return self._decision(
                    GuardAction.BLOCK,
                    GuardReasonCode.SUPPRESSED_DOMAIN,
                    "recipient domain is suppressed",
                    1.0,
                    None,
                    mailbox,
                    email,
                    "domain",
                )
            if suppression.is_legacy:
                return self._decision(
                    GuardAction.BLOCK,
                    GuardReasonCode.LEGACY_BLACKLIST,
                    "recipient is on the legacy blacklist",
                    1.0,
                    None,
                    mailbox,
                    email,
                    "email",
                )
            return self._decision(
                GuardAction.BLOCK,
                GuardReasonCode.SUPPRESSED_EMAIL,
                "recipient is suppressed",
                1.0,
                None,
                mailbox,
                email,
                "email",
            )

        # 2. Mailbox-health seam — recognize only statuses an operator set.
        #    No record → proceed; HEALTHY/WARNING/WARMING → proceed. The guard
        #    never computes a score or invents a limit here (that is P5-P7).
        if self._mailbox_health_reader is not None:
            health = self._mailbox_health_reader(mailbox)
            if health is not None:
                status = health.status
                if status is HealthStatus.PAUSED:
                    return self._decision(
                        GuardAction.PAUSE_MAILBOX,
                        GuardReasonCode.MAILBOX_PAUSED,
                        "mailbox is paused — holding all sends",
                        1.0,
                        _plus_hours(now, 12),
                        mailbox,
                        email,
                        "mailbox",
                    )
                if status is HealthStatus.DISABLED:
                    return self._decision(
                        GuardAction.PAUSE_MAILBOX,
                        GuardReasonCode.MAILBOX_DISABLED,
                        "mailbox is disabled — holding all sends",
                        1.0,
                        None,
                        mailbox,
                        email,
                        "mailbox",
                    )
                if status is HealthStatus.RESTRICTED:
                    return self._decision(
                        GuardAction.DELAY,
                        GuardReasonCode.MAILBOX_RESTRICTED,
                        "mailbox is restricted — send later",
                        0.8,
                        _plus_hours(now, 1),
                        mailbox,
                        email,
                        "mailbox",
                    )

        # 3. Existing daily-quota → MX → per-domain tier limits, via the
        #    structured twin of AntiSpamGuard.can_send. Logic is reused, not
        #    re-implemented, so the guard can never disagree with the legacy
        #    anti-spam path about quota/MX/domain state.
        verdict = self._anti_spam.pre_send_verdict(email)
        if not verdict.allowed:
            if verdict.blocked_by == "daily_quota":
                if context.override_daily_quota:
                    # The operator override lifts ONLY the daily cap. MX and the
                    # per-domain tier must still hold, so re-run the verdict
                    # skipping the daily short-circuit before allowing.
                    verdict = self._anti_spam.pre_send_verdict(
                        email, ignore_daily_quota=True
                    )
                    if verdict.blocked_by == "no_mx":
                        return self._decision(
                            GuardAction.BLOCK,
                            GuardReasonCode.NO_MX_RECORD,
                            "recipient domain has no MX record",
                            0.3,
                            None,
                            mailbox,
                            email,
                            "domain",
                        )
                    if verdict.blocked_by == "domain_quota":
                        return self._decision(
                            GuardAction.BLOCK,
                            GuardReasonCode.DOMAIN_QUOTA_EXCEEDED,
                            "recipient domain quota is exhausted",
                            0.7,
                            None,
                            mailbox,
                            email,
                            "domain",
                        )
                    return self._decision(
                        GuardAction.ALLOW,
                        GuardReasonCode.DAILY_QUOTA_OVERRIDDEN,
                        "allowed (operator daily-quota override)",
                        0.2,
                        None,
                        mailbox,
                        email,
                        "mailbox",
                    )
                return self._decision(
                    GuardAction.DELAY,
                    GuardReasonCode.DAILY_QUOTA_EXCEEDED,
                    "daily send quota is exhausted",
                    0.6,
                    _next_local_midnight(now),
                    mailbox,
                    email,
                    "mailbox",
                )
            if verdict.blocked_by == "no_mx":
                return self._decision(
                    GuardAction.BLOCK,
                    GuardReasonCode.NO_MX_RECORD,
                    "recipient domain has no MX record",
                    0.3,
                    None,
                    mailbox,
                    email,
                    "domain",
                )
            if verdict.blocked_by == "domain_quota":
                return self._decision(
                    GuardAction.BLOCK,
                    GuardReasonCode.DOMAIN_QUOTA_EXCEEDED,
                    "recipient domain quota is exhausted",
                    0.7,
                    None,
                    mailbox,
                    email,
                    "domain",
                )

        return self._decision(
            GuardAction.ALLOW,
            GuardReasonCode.ALLOW,
            "allowed",
            0.0,
            None,
            mailbox,
            email,
            None,
        )

    def _decision(
        self,
        action: GuardAction,
        reason_code: GuardReasonCode,
        message: str,
        risk_score: float,
        retry_after: Optional[str],
        mailbox: str,
        email: str,
        scope: Optional[str],
    ) -> GuardDecision:
        return GuardDecision(
            action=action,
            reason_code=reason_code,
            message=message,
            risk_score=risk_score,
            retry_after=retry_after,
            mailbox=mailbox,
            email=email,
            scope=scope,
        )


def build_sending_guard(
    conn: Any,
    *,
    mailbox: str = "default",
    anti_spam_guard: Any = None,
) -> SendingGuard:
    """Production wiring on one connection.

    Reuses the canonical suppression policy and the existing quota/MX/domain
    guard rather than duplicating their logic; the mailbox-health reader is the
    read seam for statuses an operator already set.
    """
    from emailer.rotation import AntiSpamGuard
    from services.email_reputation.repository import EmailReputationRepository
    from services.email_reputation.suppression_service import SuppressionService

    return SendingGuard(
        suppression_service=SuppressionService(conn),
        anti_spam_guard=anti_spam_guard or AntiSpamGuard(conn, mailbox=mailbox),
        mailbox_health_reader=EmailReputationRepository(conn).get_mailbox_health,
    )


def _plus_hours(now: datetime, hours: int) -> str:
    return (now + timedelta(hours=hours)).isoformat()


def _next_local_midnight(now: datetime) -> str:
    """Tomorrow 00:00:00 in the process's local timezone, as an ISO string.

    Matches when ``sending_quotas`` / ``mailbox_quotas`` reset (local calendar
    day). Naive inputs are treated as UTC so tests stay deterministic.
    """
    aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    local = aware.astimezone()
    tomorrow = local + timedelta(days=1)
    return tomorrow.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
