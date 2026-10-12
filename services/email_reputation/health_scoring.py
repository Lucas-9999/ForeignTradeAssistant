"""Deterministic Phase 5 shadow scoring for DNS audit facts.

This module provides a recommendation only. It deliberately never emits the
Guard-enforced RESTRICTED/PAUSED/DISABLED states; applying a future score to
sending policy requires an explicit later phase and operator approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from services.email_reputation.dns_audit import DomainDnsAudit
from services.email_reputation.models import (
    DnsStatus,
    DomainHealth,
    HealthStatus,
    ReputationEntityType,
    ReputationEvent,
    ReputationEventType,
    ReputationSeverity,
)
from services.email_reputation.repository import EmailReputationRepository


@dataclass(frozen=True)
class DomainHealthRecommendation:
    health_score: float
    status: HealthStatus
    reason: str


def score_domain_audit(audit: DomainDnsAudit) -> DomainHealthRecommendation:
    """Return a bounded, non-enforcing health recommendation from DNS facts."""
    score = 100.0
    failures = 0
    warnings = 0
    unknowns = 0
    for status, penalty in (
        (audit.mx_status, 35),
        (audit.a_status, 25),
        (audit.spf_status, 10),
        (audit.dmarc_status, 10),
        (audit.dkim_status, 5),
    ):
        if status is DnsStatus.FAIL:
            score -= penalty
            failures += 1
        elif status is DnsStatus.WARNING:
            score -= penalty / 2
            warnings += 1
        elif status is DnsStatus.UNKNOWN:
            # Inconclusive DNS is not a failed configuration, but it cannot
            # establish a healthy sender either. Keep a small confidence
            # penalty and surface it as WARNING without enforcing a guard.
            score -= 5
            unknowns += 1
    status = HealthStatus.WARNING if failures or warnings or unknowns else HealthStatus.HEALTHY
    reason = (
        f"dns audit: {failures} failed checks, {warnings} warnings, "
        f"{unknowns} unknown"
    )
    return DomainHealthRecommendation(
        health_score=max(0.0, min(100.0, score)), status=status, reason=reason
    )


def persist_domain_audit(
    repository: EmailReputationRepository,
    audit: DomainDnsAudit,
    *,
    checked_at: str,
) -> tuple[DomainHealth, Optional[ReputationEvent]]:
    """Persist an explicitly approved audit snapshot, never a sending action.

    The P5 score is constrained to HEALTHY/WARNING by ``score_domain_audit``.
    A change event is appended only when the score or status changed, keeping
    repeated scheduled snapshots auditable without creating event noise.
    """
    recommendation = score_domain_audit(audit)
    previous = repository.get_domain_health(audit.domain)
    snapshot = repository.upsert_domain_health(
        DomainHealth(
            domain=audit.domain,
            health_score=recommendation.health_score,
            status=recommendation.status,
            mx_status=audit.mx_status,
            spf_status=audit.spf_status,
            dkim_status=audit.dkim_status,
            dmarc_status=audit.dmarc_status,
            a_status=audit.a_status,
            ptr_status=audit.ptr_status,
            tls_status=audit.tls_status,
            sending_ip=audit.sending_ip,
            authentication_status=audit.authentication_status,
            last_checked=checked_at,
        )
    )
    changed = previous is None or _health_snapshot_changed(previous, snapshot)
    if not changed:
        return snapshot, None
    event = repository.append_reputation_event(
        entity_type=ReputationEntityType.DOMAIN,
        entity_id=audit.domain,
        event_type=ReputationEventType.DOMAIN_HEALTH_CHANGED,
        severity=(
            ReputationSeverity.WARNING
            if snapshot.status is HealthStatus.WARNING
            else ReputationSeverity.INFO
        ),
        old_score=previous.health_score if previous else None,
        new_score=snapshot.health_score,
        reason=recommendation.reason,
        metadata={
            "status": snapshot.status.value,
            "mx_status": snapshot.mx_status.value,
            "spf_status": snapshot.spf_status.value,
            "dkim_status": snapshot.dkim_status.value,
            "dmarc_status": snapshot.dmarc_status.value,
            "a_status": snapshot.a_status.value,
            "ptr_status": snapshot.ptr_status.value,
            "tls_status": snapshot.tls_status.value,
            "authentication_status": snapshot.authentication_status.value,
            "sending_ip": snapshot.sending_ip,
        },
    )
    return snapshot, event


def _health_snapshot_changed(previous: DomainHealth, current: DomainHealth) -> bool:
    """Exclude only observation timestamps when deciding whether to audit a change."""
    return any(
        getattr(previous, field) != getattr(current, field)
        for field in (
            "health_score",
            "status",
            "mx_status",
            "spf_status",
            "dkim_status",
            "dmarc_status",
            "a_status",
            "ptr_status",
            "tls_status",
            "authentication_status",
            "sending_ip",
        )
    )
