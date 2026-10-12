"""Phase 7 shadow adaptive-throttle recommendations.

The module deliberately returns advice only.  It never sleeps, mutates quota
rows, persists a mailbox state, or calls SendingGuard; production enforcement
requires separately approved P7 integration after sufficient observed data.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from services.email_reputation.models import HealthStatus, RecipientDomainMetric, RiskLevel
from services.email_reputation.reputation_engine import ReputationRecommendation


class ThrottleAction(str, Enum):
    CONTINUE = "continue"
    DELAY = "delay"
    PAUSE = "pause"


@dataclass(frozen=True)
class ThrottleAdvice:
    action: ThrottleAction
    delay_seconds: int
    reason: str
    enforcing: bool = False


@dataclass(frozen=True)
class ProviderThrottleInput:
    provider: str
    rate_limited: bool
    retry_after_seconds: Optional[int] = None


class AdaptiveThrottle:
    """Small, pure interface for mailbox, provider and recipient-MX advice."""

    def for_mailbox(self, recommendation: ReputationRecommendation) -> ThrottleAdvice:
        if recommendation.status in {HealthStatus.PAUSED, HealthStatus.DISABLED}:
            return _pause("mailbox reputation recommends pause review")
        if recommendation.status is HealthStatus.RESTRICTED:
            return _delay(900, "mailbox reputation recommends restricted rate")
        if recommendation.status is HealthStatus.WARNING:
            return _delay(60, "mailbox reputation recommends cautious rate")
        return _continue("mailbox reputation is healthy")

    def for_recipient_domain(self, metric: RecipientDomainMetric) -> ThrottleAdvice:
        level = _escalated_risk(metric.risk_level, metric.deferral_rate)
        if level is RiskLevel.CRITICAL:
            return _pause("recipient domain / MX risk is critical")
        if level is RiskLevel.HIGH:
            return _delay(300, "recipient domain / MX risk is high")
        if level is RiskLevel.MEDIUM:
            return _delay(30, "recipient domain / MX risk is elevated")
        return _continue("recipient domain / MX risk is low")

    def for_provider(self, facts: ProviderThrottleInput) -> ThrottleAdvice:
        if not facts.rate_limited:
            return _continue("provider has no rate-limit signal")
        retry_after = facts.retry_after_seconds or 300
        return _delay(max(1, retry_after), "provider rate-limit signal")


def _escalated_risk(level: RiskLevel, deferral_rate: float) -> RiskLevel:
    if not 0 <= deferral_rate <= 1:
        raise ValueError("deferral_rate must be between 0 and 1")
    if deferral_rate < 0.10 or level is RiskLevel.CRITICAL:
        return level
    return {
        RiskLevel.LOW: RiskLevel.MEDIUM,
        RiskLevel.MEDIUM: RiskLevel.HIGH,
        RiskLevel.HIGH: RiskLevel.CRITICAL,
        RiskLevel.CRITICAL: RiskLevel.CRITICAL,
    }[level]


def _continue(reason: str) -> ThrottleAdvice:
    return ThrottleAdvice(ThrottleAction.CONTINUE, 0, reason)


def _delay(seconds: int, reason: str) -> ThrottleAdvice:
    return ThrottleAdvice(ThrottleAction.DELAY, seconds, reason)


def _pause(reason: str) -> ThrottleAdvice:
    return ThrottleAdvice(ThrottleAction.PAUSE, 3600, reason)
