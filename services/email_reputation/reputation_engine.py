"""Phase 6 reputation recommendations, intentionally shadow-only.

This deep module turns explicit mailbox/domain/IP facts into deterministic
health recommendations.  It neither reads credentials nor writes the health
tables: callers must opt into any persistence in a later, separately approved
phase.  In particular, a recommended ``RESTRICTED`` or ``PAUSED`` status is
not a SendingGuard instruction.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.email_reputation.models import DnsStatus, DomainHealth, HealthStatus


@dataclass(frozen=True)
class ReputationRecommendation:
    health_score: float
    status: HealthStatus
    reason: str
    enforcing: bool = False


@dataclass(frozen=True)
class MailboxReputationInput:
    mailbox: str
    domain_score: float
    hard_bounce_rate: float
    soft_bounce_rate: float
    complaint_rate: float


@dataclass(frozen=True)
class SendingIpReputationInput:
    sending_ip: str
    ptr_status: DnsStatus
    tls_status: DnsStatus
    authentication_status: DnsStatus


class ReputationEngine:
    """Pure recommendation interface for the three P6 reputation objects."""

    def evaluate_mailbox(
        self, facts: MailboxReputationInput
    ) -> ReputationRecommendation:
        _validate_rate(facts.hard_bounce_rate, "hard_bounce_rate")
        _validate_rate(facts.soft_bounce_rate, "soft_bounce_rate")
        _validate_rate(facts.complaint_rate, "complaint_rate")
        score = 100.0
        reasons: list[str] = []
        if facts.hard_bounce_rate >= 0.10:
            score -= 50
            reasons.append("hard-bounce spike")
        elif facts.hard_bounce_rate >= 0.05:
            score -= 25
            reasons.append("elevated hard-bounce rate")
        elif facts.hard_bounce_rate >= 0.02:
            score -= 10
            reasons.append("hard-bounce warning")
        if facts.soft_bounce_rate >= 0.10:
            score -= 15
            reasons.append("elevated soft-bounce rate")
        elif facts.soft_bounce_rate >= 0.05:
            score -= 8
            reasons.append("soft-bounce warning")
        if facts.complaint_rate >= 0.01:
            score -= 80
            reasons.append("complaint rate requires pause review")
        elif facts.complaint_rate > 0:
            score -= 35
            reasons.append("complaint warning")
        if facts.domain_score < 60:
            score -= 15
            reasons.append("sending-domain health is restricted")
        elif facts.domain_score < 80:
            score -= 5
            reasons.append("sending-domain health warning")
        return _recommend(score, reasons)

    def evaluate_domain(self, health: DomainHealth) -> ReputationRecommendation:
        return _recommend(
            health.health_score,
            [f"persisted DNS/auth status: {health.status.value}"],
        )

    def evaluate_sending_ip(
        self, facts: SendingIpReputationInput
    ) -> ReputationRecommendation:
        score = 100.0
        reasons: list[str] = []
        for label, status, fail_penalty in (
            ("PTR", facts.ptr_status, 45),
            ("TLS", facts.tls_status, 30),
            ("authentication", facts.authentication_status, 30),
        ):
            if status is DnsStatus.FAIL:
                score -= fail_penalty
                reasons.append(f"{label} failed")
            elif status is DnsStatus.WARNING:
                score -= fail_penalty / 2
                reasons.append(f"{label} warning")
            elif status is DnsStatus.UNKNOWN:
                score -= 5
                reasons.append(f"{label} unknown")
        return _recommend(score, reasons)


def _recommend(score: float, reasons: list[str]) -> ReputationRecommendation:
    bounded = max(0.0, min(100.0, score))
    if bounded <= 30:
        status = HealthStatus.PAUSED
    elif bounded < 60:
        status = HealthStatus.RESTRICTED
    elif bounded < 80 or any("unknown" in reason for reason in reasons):
        status = HealthStatus.WARNING
    else:
        status = HealthStatus.HEALTHY
    return ReputationRecommendation(
        health_score=bounded,
        status=status,
        reason="; ".join(reasons) if reasons else "no adverse reputation signals",
    )


def _validate_rate(value: float, name: str) -> None:
    if not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
