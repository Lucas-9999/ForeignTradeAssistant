"""Typed records for the Phase 1 email reputation data layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class SuppressionReason(str, Enum):
    HARD_BOUNCE = "hard_bounce"
    UNSUBSCRIBE = "unsubscribe"
    SPAM_COMPLAINT = "spam_complaint"
    INVALID_EMAIL = "invalid_email"
    DEAD_DOMAIN = "dead_domain"
    MANUAL_BLOCK = "manual_block"
    DO_NOT_CONTACT = "do_not_contact"


class ReputationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ReputationEntityType(str, Enum):
    DOMAIN = "domain"
    MAILBOX = "mailbox"
    SENDING_IP = "sending_ip"
    PROVIDER = "provider"
    RECIPIENT_DOMAIN = "recipient_domain"


class ReputationEventType(str, Enum):
    DKIM_FAILED = "DKIM_FAILED"
    HARD_BOUNCE_SPIKE = "HARD_BOUNCE_SPIKE"
    RATE_LIMITED = "RATE_LIMITED"
    MAILBOX_PAUSED = "MAILBOX_PAUSED"
    MAILBOX_RECOVERED = "MAILBOX_RECOVERED"
    MAILBOX_HEALTH_CHANGED = "MAILBOX_HEALTH_CHANGED"
    DOMAIN_HEALTH_CHANGED = "DOMAIN_HEALTH_CHANGED"
    PTR_FAILED = "PTR_FAILED"
    DMARC_FAILED = "DMARC_FAILED"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    WARNING = "warning"
    RESTRICTED = "restricted"
    PAUSED = "paused"
    WARMING = "warming"
    DISABLED = "disabled"


class DnsStatus(str, Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class SuppressionRecord:
    id: int
    email: Optional[str]
    domain: Optional[str]
    reason: SuppressionReason
    source: str
    created_at: str
    expires_at: Optional[str]
    is_permanent: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReputationEvent:
    id: int
    entity_type: ReputationEntityType
    entity_id: str
    event_type: ReputationEventType
    severity: ReputationSeverity
    old_score: Optional[float]
    new_score: Optional[float]
    reason: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class MailboxHealth:
    mailbox: str
    health_score: float
    status: HealthStatus
    daily_sent: int
    hourly_sent: int
    hard_bounce_rate: float
    soft_bounce_rate: float
    reply_rate: float
    complaint_rate: float
    last_error: Optional[str]
    last_health_check: Optional[str]
    provider: str
    sending_ip: Optional[str]
    domain: str
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class DomainHealth:
    domain: str
    health_score: float
    status: HealthStatus
    mx_status: DnsStatus
    spf_status: DnsStatus
    dkim_status: DnsStatus
    dmarc_status: DnsStatus
    a_status: DnsStatus
    ptr_status: DnsStatus
    tls_status: DnsStatus
    sending_ip: Optional[str]
    authentication_status: DnsStatus
    last_checked: Optional[str]
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class RecipientDomainMetric:
    domain: str
    metric_date: str
    metric_hour: int
    mx_provider: Optional[str]
    hourly_sent: int
    daily_sent: int
    bounce_rate: float
    deferral_rate: float
    last_smtp_error: Optional[str]
    risk_level: RiskLevel
    id: Optional[int] = None
    updated_at: Optional[str] = None
