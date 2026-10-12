"""Phase 6 shadow reputation recommendations: pure, deterministic, no DB."""

from services.email_reputation.models import DnsStatus, DomainHealth, HealthStatus
from services.email_reputation.reputation_engine import (
    MailboxReputationInput,
    ReputationEngine,
    SendingIpReputationInput,
)


def test_clean_mailbox_is_healthy():
    recommendation = ReputationEngine().evaluate_mailbox(
        MailboxReputationInput(
            mailbox="default",
            domain_score=100,
            hard_bounce_rate=0.0,
            soft_bounce_rate=0.0,
            complaint_rate=0.0,
        )
    )

    assert recommendation.health_score == 100
    assert recommendation.status is HealthStatus.HEALTHY
    assert recommendation.enforcing is False


def test_hard_bounce_spike_becomes_restricted_shadow_recommendation():
    recommendation = ReputationEngine().evaluate_mailbox(
        MailboxReputationInput(
            mailbox="default",
            domain_score=100,
            hard_bounce_rate=0.10,
            soft_bounce_rate=0.0,
            complaint_rate=0.0,
        )
    )

    assert recommendation.health_score == 50
    assert recommendation.status is HealthStatus.RESTRICTED
    assert recommendation.enforcing is False
    assert "hard-bounce spike" in recommendation.reason


def test_complaint_rate_can_recommend_pause_but_never_enforces_it():
    recommendation = ReputationEngine().evaluate_mailbox(
        MailboxReputationInput(
            mailbox="default",
            domain_score=100,
            hard_bounce_rate=0.0,
            soft_bounce_rate=0.0,
            complaint_rate=0.01,
        )
    )

    assert recommendation.health_score == 20
    assert recommendation.status is HealthStatus.PAUSED
    assert recommendation.enforcing is False


def test_domain_recommendation_uses_persisted_dns_score_without_rewriting_it():
    health = DomainHealth(
        domain="example.com",
        health_score=55,
        status=HealthStatus.WARNING,
        mx_status=DnsStatus.PASS,
        spf_status=DnsStatus.WARNING,
        dkim_status=DnsStatus.UNKNOWN,
        dmarc_status=DnsStatus.WARNING,
        a_status=DnsStatus.PASS,
        ptr_status=DnsStatus.UNKNOWN,
        tls_status=DnsStatus.UNKNOWN,
        sending_ip=None,
        authentication_status=DnsStatus.WARNING,
        last_checked="2026-08-22 10:00:00",
    )

    recommendation = ReputationEngine().evaluate_domain(health)

    assert recommendation.health_score == 55
    assert recommendation.status is HealthStatus.RESTRICTED
    assert recommendation.enforcing is False


def test_ip_ptr_failure_is_restricted_but_network_unknown_is_only_warning():
    engine = ReputationEngine()
    failed = engine.evaluate_sending_ip(
        SendingIpReputationInput(
            sending_ip="192.0.2.10",
            ptr_status=DnsStatus.FAIL,
            tls_status=DnsStatus.PASS,
            authentication_status=DnsStatus.PASS,
        )
    )
    unknown = engine.evaluate_sending_ip(
        SendingIpReputationInput(
            sending_ip="192.0.2.10",
            ptr_status=DnsStatus.UNKNOWN,
            tls_status=DnsStatus.UNKNOWN,
            authentication_status=DnsStatus.UNKNOWN,
        )
    )

    assert failed.status is HealthStatus.RESTRICTED
    assert unknown.status is HealthStatus.WARNING
    assert unknown.health_score > failed.health_score
