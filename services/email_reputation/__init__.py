"""Email reputation data and pure bounce intelligence (Phases 1-2)."""

from services.email_reputation.bounce_classifier import (
    BounceCategory,
    BounceClassification,
    classify_bounce,
)
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
from services.email_reputation.repository import EmailReputationRepository
from services.email_reputation.smtp_error_parser import (
    ParsedSmtpError,
    SmtpDiagnosticEvent,
    SmtpDiagnosticSignal,
    build_smtp_diagnostic_event,
    parse_smtp_error,
)

__all__ = [
    "EmailReputationRepository",
    "BounceCategory",
    "BounceClassification",
    "DnsStatus",
    "DomainHealth",
    "HealthStatus",
    "MailboxHealth",
    "RecipientDomainMetric",
    "ReputationEntityType",
    "ReputationEvent",
    "ReputationEventType",
    "ReputationSeverity",
    "RiskLevel",
    "SuppressionReason",
    "SuppressionRecord",
    "ParsedSmtpError",
    "SmtpDiagnosticEvent",
    "SmtpDiagnosticSignal",
    "build_smtp_diagnostic_event",
    "classify_bounce",
    "parse_smtp_error",
]
