import sqlite3

import pytest

from crm.db import build_database
from services.email_reputation.models import (
    DnsStatus,
    DomainHealth,
    HealthStatus,
    MailboxHealth,
    RecipientDomainMetric,
    ReputationSeverity,
    ReputationEntityType,
    ReputationEventType,
    RiskLevel,
    SuppressionReason,
)
from services.email_reputation.repository import EmailReputationRepository


def _repository(tmp_path) -> tuple[sqlite3.Connection, EmailReputationRepository]:
    conn = build_database(tmp_path / "reputation.db")
    return conn, EmailReputationRepository(conn)


def test_operator_can_suppress_and_find_a_normalized_email(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        created = repository.suppress_email(
            " Buyer@Example.COM ",
            reason=SuppressionReason.HARD_BOUNCE,
            source="imap",
            metadata={"smtp_code": "550"},
        )

        found = repository.find_active_suppression("buyer@example.com")

        assert created.email == "buyer@example.com"
        assert created.reason is SuppressionReason.HARD_BOUNCE
        assert created.metadata == {"smtp_code": "550"}
        assert found == created
    finally:
        conn.close()


def test_resuppressing_email_updates_one_canonical_record(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        original = repository.suppress_email(
            "buyer@example.com",
            reason=SuppressionReason.INVALID_EMAIL,
            source="verification",
            is_permanent=False,
            expires_at="2026-09-01 00:00:00",
        )
        updated = repository.suppress_email(
            "BUYER@example.com",
            reason=SuppressionReason.HARD_BOUNCE,
            source="imap",
            metadata={"classification": "hard"},
        )

        count = conn.execute(
            "SELECT COUNT(*) FROM email_suppressions"
        ).fetchone()[0]
        assert updated.id == original.id
        assert updated.reason is SuppressionReason.HARD_BOUNCE
        assert updated.is_permanent is True
        assert updated.metadata == {"classification": "hard"}
        assert count == 1
    finally:
        conn.close()


def test_domain_suppression_applies_and_expired_email_suppression_does_not(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        repository.suppress_domain(
            " Example.COM ",
            reason=SuppressionReason.DEAD_DOMAIN,
            source="mx_cleanup",
        )
        repository.suppress_email(
            "expired@other.test",
            reason=SuppressionReason.INVALID_EMAIL,
            source="verification",
            is_permanent=False,
            expires_at="2000-01-01 00:00:00",
        )

        domain_match = repository.find_active_suppression("anyone@example.com")
        expired_match = repository.find_active_suppression("expired@other.test")

        assert domain_match is not None
        assert domain_match.email is None
        assert domain_match.domain == "example.com"
        assert domain_match.reason is SuppressionReason.DEAD_DOMAIN
        assert expired_match is None
    finally:
        conn.close()


def test_temporary_suppression_requires_an_expiry(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="expires_at"):
            repository.suppress_email(
                "buyer@example.com",
                reason=SuppressionReason.INVALID_EMAIL,
                source="verification",
                is_permanent=False,
            )

        assert conn.execute("SELECT COUNT(*) FROM email_suppressions").fetchone()[0] == 0
    finally:
        conn.close()


def test_temporary_suppression_rejects_malformed_expiry(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="valid ISO timestamp"):
            repository.suppress_email(
                "buyer@example.com",
                reason=SuppressionReason.INVALID_EMAIL,
                source="verification",
                is_permanent=False,
                expires_at="not-a-date",
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("email", "a@b@example.com"),
        ("domain", "https://example.com"),
        ("domain", "bad domain.com"),
    ],
)
def test_invalid_recipient_identifiers_are_rejected(tmp_path, kind, value):
    conn, repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError):
            if kind == "email":
                repository.find_active_suppression(value)
            else:
                repository.suppress_domain(
                    value,
                    reason=SuppressionReason.DEAD_DOMAIN,
                    source="validation",
                )
    finally:
        conn.close()


def test_reputation_events_are_appended_with_typed_metadata(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        created = repository.append_reputation_event(
            entity_type=ReputationEntityType.MAILBOX,
            entity_id="default",
            event_type=ReputationEventType.MAILBOX_HEALTH_CHANGED,
            severity=ReputationSeverity.WARNING,
            old_score=96,
            new_score=82,
            reason="hard bounce rate crossed warning threshold",
            metadata={"window_days": 7},
        )

        events = repository.list_reputation_events(
            entity_type=ReputationEntityType.MAILBOX, entity_id="default"
        )

        assert events == [created]
        assert created.severity is ReputationSeverity.WARNING
        assert created.old_score == 96
        assert created.new_score == 82
        assert created.metadata == {"window_days": 7}
    finally:
        conn.close()


@pytest.mark.parametrize("secret_key", ["api_key", "smtpPassword", "apiKey", "accessToken"])
def test_secret_bearing_metadata_is_rejected_before_serialization(
    tmp_path, secret_key
):
    conn, repository = _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="sensitive metadata key"):
            repository.suppress_email(
                "buyer@example.com",
                reason=SuppressionReason.HARD_BOUNCE,
                source="imap",
                metadata={"smtp": {secret_key: "must-not-be-stored"}},
            )

        assert conn.execute("SELECT COUNT(*) FROM email_suppressions").fetchone()[0] == 0
    finally:
        conn.close()


def test_secret_fragments_are_redacted_from_persisted_error_text(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO mailboxes
               (name, display_name, smtp_host, smtp_port, use_tls, username,
                from_email, from_name, is_active, source)
               VALUES ('secret-test', 'Test', 'smtp.invalid', 465, 0,
                       'sender@example.com', 'sender@example.com', 'Test', 1, 'test')"""
        )
        conn.commit()
        snapshot = MailboxHealth(
            mailbox="secret-test",
            health_score=0,
            status=HealthStatus.DISABLED,
            daily_sent=0,
            hourly_sent=0,
            hard_bounce_rate=0,
            soft_bounce_rate=0,
            reply_rate=0,
            complaint_rate=0,
            last_error=(
                'authentication failed password=hunter2 api_key=abc123 '
                'Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ== '
                '{"password":"json-secret"}'
            ),
            last_health_check="2026-08-18 09:00:00",
            provider="test",
            sending_ip=None,
            domain="example.com",
        )

        written = repository.upsert_mailbox_health(snapshot)

        assert "hunter2" not in (written.last_error or "")
        assert "abc123" not in (written.last_error or "")
        assert "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" not in (written.last_error or "")
        assert "json-secret" not in (written.last_error or "")
        assert "[REDACTED]" in (written.last_error or "")
    finally:
        conn.close()


def test_mailbox_health_snapshot_can_be_upserted_and_read(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO mailboxes
               (name, display_name, smtp_host, smtp_port, use_tls, username,
                from_email, from_name, is_active, source)
               VALUES ('test-mailbox', 'Test', 'smtp.invalid', 465, 0,
                       'sender@example.com', 'sender@example.com', 'Test', 1, 'test')"""
        )
        conn.commit()
        snapshot = MailboxHealth(
            mailbox="test-mailbox",
            health_score=82,
            status=HealthStatus.WARNING,
            daily_sent=17,
            hourly_sent=3,
            hard_bounce_rate=0.01,
            soft_bounce_rate=0.02,
            reply_rate=0.04,
            complaint_rate=0.0,
            last_error="rate limited",
            last_health_check="2026-08-18 09:00:00",
            provider="test",
            sending_ip=None,
            domain="example.com",
        )

        written = repository.upsert_mailbox_health(snapshot)
        found = repository.get_mailbox_health("test-mailbox")

        assert found == written
        assert found.status is HealthStatus.WARNING
        assert found.health_score == 82
        assert found.daily_sent == 17
        assert found.provider == "test"
    finally:
        conn.close()


def test_domain_health_snapshot_preserves_authentication_verdicts(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        snapshot = DomainHealth(
            domain=" Example.COM. ",
            health_score=78,
            status=HealthStatus.WARNING,
            mx_status=DnsStatus.PASS,
            spf_status=DnsStatus.WARNING,
            dkim_status=DnsStatus.UNKNOWN,
            dmarc_status=DnsStatus.FAIL,
            a_status=DnsStatus.PASS,
            ptr_status=DnsStatus.UNKNOWN,
            tls_status=DnsStatus.PASS,
            sending_ip=None,
            authentication_status=DnsStatus.WARNING,
            last_checked="2026-08-18 09:05:00",
        )

        written = repository.upsert_domain_health(snapshot)
        found = repository.get_domain_health("example.com")

        assert found == written
        assert found.domain == "example.com"
        assert found.dmarc_status is DnsStatus.FAIL
        assert found.authentication_status is DnsStatus.WARNING
    finally:
        conn.close()


def test_recipient_domain_hour_bucket_is_upserted_without_duplicate(tmp_path):
    conn, repository = _repository(tmp_path)
    try:
        first = RecipientDomainMetric(
            domain="Gmail.COM",
            metric_date="2026-08-18",
            metric_hour=9,
            mx_provider="google",
            hourly_sent=3,
            daily_sent=12,
            bounce_rate=0.0,
            deferral_rate=0.0,
            last_smtp_error=None,
            risk_level=RiskLevel.LOW,
        )
        revised = RecipientDomainMetric(
            domain="gmail.com",
            metric_date="2026-08-18",
            metric_hour=9,
            mx_provider="google",
            hourly_sent=5,
            daily_sent=14,
            bounce_rate=0.01,
            deferral_rate=0.02,
            last_smtp_error="temporary deferral",
            risk_level=RiskLevel.MEDIUM,
        )

        original = repository.upsert_recipient_domain_metric(first)
        updated = repository.upsert_recipient_domain_metric(revised)
        found = repository.get_recipient_domain_metric("gmail.com", "2026-08-18", 9)

        assert original.id == updated.id
        assert found == updated
        assert found.domain == "gmail.com"
        assert found.hourly_sent == 5
        assert found.risk_level is RiskLevel.MEDIUM
        assert conn.execute("SELECT COUNT(*) FROM recipient_domain_metrics").fetchone()[0] == 1
    finally:
        conn.close()
