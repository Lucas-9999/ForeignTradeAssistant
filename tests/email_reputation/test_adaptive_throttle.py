"""Phase 7 throttle advice is pure and never delays a real send."""

from services.email_reputation.adaptive_throttle import (
    AdaptiveThrottle,
    ProviderThrottleInput,
    ThrottleAction,
)
from services.email_reputation.models import (
    HealthStatus,
    RecipientDomainMetric,
    RiskLevel,
)
from services.email_reputation.reputation_engine import ReputationRecommendation


def test_healthy_mailbox_has_no_throttle_advice():
    advice = AdaptiveThrottle().for_mailbox(
        ReputationRecommendation(100, HealthStatus.HEALTHY, "clean")
    )

    assert advice.action is ThrottleAction.CONTINUE
    assert advice.delay_seconds == 0
    assert advice.enforcing is False


def test_restricted_mailbox_recommends_delay_not_an_actual_pause():
    advice = AdaptiveThrottle().for_mailbox(
        ReputationRecommendation(55, HealthStatus.RESTRICTED, "bounce spike")
    )

    assert advice.action is ThrottleAction.DELAY
    assert advice.delay_seconds == 900
    assert advice.enforcing is False


def test_critical_recipient_domain_recommends_pause():
    metric = RecipientDomainMetric(
        domain="gmail.com",
        metric_date="2026-08-22",
        metric_hour=10,
        mx_provider="google",
        hourly_sent=10,
        daily_sent=40,
        bounce_rate=0.0,
        deferral_rate=0.0,
        last_smtp_error=None,
        risk_level=RiskLevel.CRITICAL,
    )

    advice = AdaptiveThrottle().for_recipient_domain(metric)

    assert advice.action is ThrottleAction.PAUSE
    assert advice.delay_seconds == 3600


def test_deferral_rate_escalates_low_risk_domain_to_delay():
    metric = RecipientDomainMetric(
        domain="example.com",
        metric_date="2026-08-22",
        metric_hour=10,
        mx_provider=None,
        hourly_sent=3,
        daily_sent=8,
        bounce_rate=0.0,
        deferral_rate=0.10,
        last_smtp_error="temporary deferral",
        risk_level=RiskLevel.LOW,
    )

    advice = AdaptiveThrottle().for_recipient_domain(metric)

    assert advice.action is ThrottleAction.DELAY
    assert advice.delay_seconds == 30


def test_provider_retry_after_is_preserved_in_shadow_advice():
    advice = AdaptiveThrottle().for_provider(
        ProviderThrottleInput("zoho", rate_limited=True, retry_after_seconds=120)
    )

    assert advice.action is ThrottleAction.DELAY
    assert advice.delay_seconds == 120
