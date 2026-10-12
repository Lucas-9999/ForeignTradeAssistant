import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from crm.db import build_database
from services.email_reputation.models import SuppressionReason
from services.email_reputation.repository import EmailReputationRepository
from services.email_reputation.suppression_service import SuppressionService


def _service(tmp_path) -> tuple[sqlite3.Connection, SuppressionService]:
    conn = build_database(tmp_path / "suppression.db")
    return conn, SuppressionService(conn)


def test_active_unified_email_suppression_blocks_recipient(tmp_path):
    conn, service = _service(tmp_path)
    try:
        EmailReputationRepository(conn).suppress_email(
            "Buyer@Example.com",
            reason=SuppressionReason.UNSUBSCRIBE,
            source="imap",
        )

        decision = service.check("buyer@example.com")

        assert decision.blocked is True
        assert decision.reason is SuppressionReason.UNSUBSCRIBE
        assert decision.source == "imap"
        assert decision.scope == "email"
        assert decision.is_legacy is False
    finally:
        conn.close()


def test_legacy_blacklist_remains_an_active_suppression_source(tmp_path):
    conn, service = _service(tmp_path)
    try:
        conn.execute(
            "INSERT INTO blacklist (email, reason) VALUES (?, ?)",
            ("legacy@example.com", "unsubscribed"),
        )
        conn.commit()

        decision = service.check("LEGACY@example.com")

        assert decision.blocked is True
        assert decision.reason is SuppressionReason.UNSUBSCRIBE
        assert decision.source == "legacy_blacklist"
        assert decision.scope == "email"
        assert decision.is_legacy is True
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("legacy_reason", "expected"),
    [
        ("bounced", SuppressionReason.HARD_BOUNCE),
        ("spam complaint", SuppressionReason.SPAM_COMPLAINT),
        ("invalid email", SuppressionReason.INVALID_EMAIL),
        ("dead domain", SuppressionReason.DEAD_DOMAIN),
        ("do not contact", SuppressionReason.DO_NOT_CONTACT),
        ("operator", SuppressionReason.MANUAL_BLOCK),
    ],
)
def test_legacy_reasons_map_to_closed_suppression_vocabulary(
    tmp_path, legacy_reason, expected
):
    conn, service = _service(tmp_path)
    try:
        conn.execute(
            "INSERT INTO blacklist (email, reason) VALUES (?, ?)",
            ("legacy@example.com", legacy_reason),
        )
        conn.commit()

        assert service.check("legacy@example.com").reason is expected
    finally:
        conn.close()


def test_service_is_the_public_write_seam_for_email_suppression(tmp_path):
    conn, service = _service(tmp_path)
    try:
        created = service.suppress_email(
            "blocked@example.com",
            reason=SuppressionReason.MANUAL_BLOCK,
            source="operator",
        )

        decision = service.check("blocked@example.com")

        assert created.email == "blocked@example.com"
        assert decision.blocked is True
        assert decision.reason is SuppressionReason.MANUAL_BLOCK
    finally:
        conn.close()


def test_domain_suppression_blocks_every_mailbox_at_that_domain(tmp_path):
    conn, service = _service(tmp_path)
    try:
        service.suppress_domain(
            "Example.com",
            reason=SuppressionReason.DEAD_DOMAIN,
            source="dns_audit",
        )

        decision = service.check("anyone@example.com")

        assert decision.blocked is True
        assert decision.reason is SuppressionReason.DEAD_DOMAIN
        assert decision.scope == "domain"
    finally:
        conn.close()


def test_confirmed_hard_bounce_creates_permanent_email_suppression(tmp_path):
    conn, service = _service(tmp_path)
    try:
        created = service.suppress_from_smtp_error(
            "buyer@example.com",
            "550 5.1.1 user unknown",
            provider="netease",
        )

        assert created is not None
        assert created.reason is SuppressionReason.HARD_BOUNCE
        assert created.is_permanent is True
        assert created.source == "smtp:netease"
        assert created.metadata["recipient_domain"] == "example.com"
        assert created.metadata["provider"] == "netease"
        assert created.metadata["observed_at"].endswith("Z")
        assert service.check("buyer@example.com").blocked is True
    finally:
        conn.close()


@pytest.mark.parametrize(
    "diagnostic",
    [
        "552 5.2.2 mailbox full",
        "554 5.7.1 policy rejection",
        "550 5.7.1 IP reputation blocked",
        "Recipient refused: buyer@example.com",
    ],
)
def test_non_recipient_failures_never_create_suppression(tmp_path, diagnostic):
    conn, service = _service(tmp_path)
    try:
        created = service.suppress_from_smtp_error(
            "buyer@example.com",
            diagnostic,
            provider="test",
        )

        assert created is None
        assert service.check("buyer@example.com").blocked is False
        assert conn.execute("SELECT COUNT(*) FROM email_suppressions").fetchone()[0] == 0
    finally:
        conn.close()


def test_malformed_recipient_is_blocked_without_persisting_a_record(tmp_path):
    conn, service = _service(tmp_path)
    try:
        decision = service.check("not-an-email")

        assert decision.blocked is True
        assert decision.reason is SuppressionReason.INVALID_EMAIL
        assert decision.source == "validation"
        assert conn.execute("SELECT COUNT(*) FROM email_suppressions").fetchone()[0] == 0
    finally:
        conn.close()


def test_hard_bounce_signal_after_output_limit_is_still_classified(tmp_path):
    conn, service = _service(tmp_path)
    try:
        created = service.suppress_from_smtp_error(
            "buyer@example.com",
            ("temporary diagnostic context " * 100) + " 550 5.1.1 user unknown",
            provider="test",
        )

        assert created is not None
        assert created.reason is SuppressionReason.HARD_BOUNCE
        assert service.check("buyer@example.com").blocked is True
    finally:
        conn.close()


@pytest.mark.parametrize("scope", ["email", "domain"])
def test_concurrent_suppression_writes_are_atomic_and_idempotent(tmp_path, scope):
    db_path = tmp_path / f"concurrent-{scope}.db"
    build_database(db_path).close()
    barrier = threading.Barrier(2)

    def write(reason):
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            service = SuppressionService(conn)
            barrier.wait(timeout=5)
            if scope == "email":
                return service.suppress_email(
                    "buyer@example.com",
                    reason=reason,
                    source="concurrency_test",
                )
            return service.suppress_domain(
                "example.com",
                reason=reason,
                source="concurrency_test",
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(
            pool.map(
                write,
                [SuppressionReason.MANUAL_BLOCK, SuppressionReason.DO_NOT_CONTACT],
            )
        )

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM email_suppressions").fetchone()[0]
    conn.close()
    assert records[0].id == records[1].id
    assert count == 1
