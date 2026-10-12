"""send_batch → SendingGuard integration tests (Phase 4).

Every lead in the canonical batch must pass the guard before the transport;
a BLOCK / DELAY / PAUSE_MAILBOX must stop the batch (mailbox-level) or skip the
lead (recipient-level) without ever touching SMTP and without counting as an
SMTP delivery failure. Quota / communication / claim semantics are unchanged.
"""

import sqlite3

import pytest

from crm.db import build_database
from emailer.batch import send_batch
from services.email_reputation.models import (
    HealthStatus,
    MailboxHealth,
    SuppressionReason,
)
from services.email_reputation.repository import EmailReputationRepository
from services.email_reputation.suppression_service import SuppressionService


@pytest.fixture
def batch_db(tmp_path, monkeypatch):
    import config

    db_path = tmp_path / "batch_guard.db"
    build_database(db_path).close()
    # send_batch reads config.DB_PATH directly — point it at this temp DB.
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    monkeypatch.setattr("emailer.rotation.compute_send_delay", lambda: 0)
    return db_path


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _add_lead(db_path, email="buyer@example.com"):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO leads (company_name, contact_name, email, country,
                              industry, source, status, discovered_at)
           VALUES ('Buyer Co', 'Buyer', ?, 'USA', 'Automotive', 'manual',
                   'new', datetime('now', '-1 day'))""",
        (email,),
    )
    conn.commit()
    lead_id = cur.lastrowid
    conn.close()
    return lead_id


def _forbidden_transport(**kwargs):
    raise AssertionError("guarded batch send must never reach SMTP")


def _ok_transport(**kwargs):
    return True, "sent", "batch-msg-1"


def test_suppressed_lead_never_reaches_smtp(batch_db, monkeypatch):
    lead_id = _add_lead(batch_db, "blocked@example.com")
    conn = sqlite3.connect(batch_db)
    SuppressionService(conn).suppress_email(
        "blocked@example.com",
        reason=SuppressionReason.MANUAL_BLOCK,
        source="test",
    )
    conn.close()

    monkeypatch.setattr("emailer.sender.send_email_sync", _forbidden_transport)

    result = send_batch(limit=5, lead_ids=[lead_id])

    assert result["sent"] == 0
    assert result["errors"] == 0
    assert result["skipped"] == 1
    assert result["details"][0]["status"] == "skipped"
    # The lead was not claimed and not contacted.
    conn = _connect(batch_db)
    row = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
    comms = conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0]
    conn.close()
    assert row["status"] == "new"
    assert comms == 0


def test_unlocked_batch_without_exact_authorization_stays_non_sending(batch_db, monkeypatch):
    lead_id = _add_lead(batch_db, "buyer@example.com")
    calls = []

    def ok_transport(**kwargs):
        calls.append(kwargs["to_email"])
        return True, "sent", "batch-msg-2"

    monkeypatch.setattr("emailer.sender.send_email_sync", ok_transport)

    result = send_batch(limit=5, lead_ids=[lead_id])

    assert result["sent"] == 0
    assert result["skipped"] == 1
    assert result["details"][0]["status"] == "authorization_dry_run"
    assert calls == []
    # The temporary claim is rolled back; no delivery state is recorded.
    conn = _connect(batch_db)
    row = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
    quota = conn.execute(
        "SELECT email_count FROM sending_quotas WHERE date = date('now')"
    ).fetchone()
    comms = conn.execute(
        "SELECT COUNT(*) FROM communications WHERE lead_id = ?", (lead_id,)
    ).fetchone()
    conn.close()
    assert row["status"] == "new"
    assert quota["email_count"] == 0
    assert comms[0] == 0


def test_operator_enabled_batch_sends_only_after_existing_guard_allows(batch_db, monkeypatch):
    """P13 restores the canonical normal batch path, never a transport bypass."""
    import config

    lead_id = _add_lead(batch_db, "buyer@example.com")
    calls = []

    def ok_transport(**kwargs):
        calls.append(kwargs["to_email"])
        return True, "sent", "batch-p13-msg"

    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    monkeypatch.setattr(config, "OUTBOUND_DELIVERY_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "emailer.send_core.normal_outbound_delivery_ready",
        lambda _profile: (True, "IMAP monitor ready"),
    )
    monkeypatch.setattr("emailer.sender.send_email_sync", ok_transport)

    result = send_batch(limit=5, lead_ids=[lead_id])

    assert result["sent"] == 1
    assert result["errors"] == 0
    assert calls == ["buyer@example.com"]


def test_operator_enabled_batch_keeps_suppressed_recipient_off_smtp(batch_db, monkeypatch):
    """P13 production enablement cannot weaken the existing suppression contract."""
    import config

    lead_id = _add_lead(batch_db, "blocked@example.com")
    conn = sqlite3.connect(batch_db)
    SuppressionService(conn).suppress_email(
        "blocked@example.com",
        reason=SuppressionReason.HARD_BOUNCE,
        source="test",
    )
    conn.close()

    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    monkeypatch.setattr(config, "OUTBOUND_DELIVERY_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "emailer.send_core.normal_outbound_delivery_ready",
        lambda _profile: (True, "IMAP monitor ready"),
    )
    monkeypatch.setattr("emailer.sender.send_email_sync", _forbidden_transport)

    result = send_batch(limit=5, lead_ids=[lead_id])

    assert result["sent"] == 0
    assert result["errors"] == 0
    assert result["skipped"] == 1


def test_global_dry_run_keeps_allowed_batch_lead_and_quota_unchanged(
    batch_db, monkeypatch, caplog
):
    """P10 must stop before the batch claim, not merely before SMTP."""
    import config

    lead_id = _add_lead(batch_db, "preview@example.com")
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", True, raising=False)
    caplog.set_level("INFO", logger="emailer.send_core")
    monkeypatch.setattr("emailer.sender.send_email_sync", _forbidden_transport)

    result = send_batch(limit=5, lead_ids=[lead_id])

    assert result["sent"] == 0
    assert result["errors"] == 0
    assert result["skipped"] == 1
    assert result["details"][0]["status"] == "global_dry_run"
    assert any("email_global_dry_run" in item.message for item in caplog.records)
    conn = _connect(batch_db)
    lead = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
    quota = conn.execute(
        "SELECT email_count FROM sending_quotas WHERE date = date('now')"
    ).fetchone()
    communications = conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0]
    conn.close()
    assert lead["status"] == "new"
    assert quota["email_count"] == 0
    assert communications == 0


def test_daily_cap_stops_batch_without_smtp(batch_db, monkeypatch):
    from datetime import date

    lead_id = _add_lead(batch_db)
    conn = sqlite3.connect(batch_db)
    # Seed today's quota row at the cap BEFORE send_batch runs — AntiSpamGuard's
    # constructor only INSERT OR IGNOREs a fresh 0-row when none exists, and the
    # guard reads the row keyed by local date (date.today().isoformat()).
    conn.execute(
        "INSERT INTO sending_quotas (date, email_count, domain_counts) "
        "VALUES (?, 200, '{}')",
        (date.today().isoformat(),),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr("emailer.sender.send_email_sync", _forbidden_transport)

    result = send_batch(limit=5, lead_ids=[lead_id])

    assert result["sent"] == 0
    assert result["skipped"] == 1
    assert "exhausted" in result["details"][0]["reason"].lower()
    conn = _connect(batch_db)
    row = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    assert row["status"] == "new"


def test_paused_mailbox_halts_batch_without_smtp(batch_db, monkeypatch):
    lead_id = _add_lead(batch_db)
    conn = sqlite3.connect(batch_db)
    EmailReputationRepository(conn).upsert_mailbox_health(
        MailboxHealth(
            mailbox="default",
            health_score=5.0,
            status=HealthStatus.PAUSED,
            daily_sent=0,
            hourly_sent=0,
            hard_bounce_rate=0.0,
            soft_bounce_rate=0.0,
            reply_rate=0.0,
            complaint_rate=0.0,
            last_error=None,
            last_health_check=None,
            provider="test",
            sending_ip=None,
            domain="example.com",
        )
    )
    conn.close()

    monkeypatch.setattr("emailer.sender.send_email_sync", _forbidden_transport)

    result = send_batch(limit=5, lead_ids=[lead_id])

    assert result["sent"] == 0
    assert result["skipped"] == 1
    assert result["details"][0]["status"] == "skipped"
