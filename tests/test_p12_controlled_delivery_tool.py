"""P12 operator command: fake transport only, never real SMTP in tests."""

from datetime import datetime, timedelta, timezone

from crm.db import build_database
from services.email_reputation.delivery_authorization import (
    AuthorizationStatus,
    DeliveryAuthorizationRepository,
    Purpose,
)


def _db(tmp_path, monkeypatch):
    import config

    path = tmp_path / "p12-tool.db"
    conn = build_database(path)
    conn.execute(
        "INSERT INTO leads (id, company_name, email, status) "
        "VALUES (7, 'Buyer', 'buyer@example.com', 'new')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    return path


def _authorization(path, *, mailbox="zoho"):
    import sqlite3

    conn = sqlite3.connect(path)
    now = datetime.now(timezone.utc)
    try:
        item = DeliveryAuthorizationRepository(conn).create(
            lead_id=7, email="buyer@example.com", mailbox=mailbox,
            purpose=Purpose.CONTROLLED_TEST, expires_at=now + timedelta(minutes=10),
            created_by="test-operator", approval_note="fake transport test", now=now,
        )
        return item.id
    finally:
        conn.close()


def test_controlled_command_consumes_exact_authorization_then_records_fake_success(tmp_path, monkeypatch):
    from tools.p12_controlled_delivery import run_controlled_delivery

    path = _db(tmp_path, monkeypatch)
    authorization_id = _authorization(path)
    calls = []
    result = run_controlled_delivery(
        lead_id=7, mailbox="zoho", purpose=Purpose.CONTROLLED_TEST,
        confirmation="SEND-7", transport=lambda **kwargs: (calls.append(kwargs) or (True, "sent", "fake-id")),
    )

    assert result.success is True
    assert result.dry_run is False
    assert calls and calls[0]["to_email"] == "buyer@example.com"

    import sqlite3
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT status FROM delivery_authorizations WHERE id=?", (authorization_id,)).fetchone()[0] == AuthorizationStatus.CONSUMED.value
        assert conn.execute("SELECT status FROM leads WHERE id=7").fetchone()[0] == "contacted"
        assert conn.execute("SELECT COUNT(*) FROM communications WHERE lead_id=7").fetchone()[0] == 1
    finally:
        conn.close()


def test_controlled_command_rejects_bad_confirmation_without_consuming_or_writing(tmp_path, monkeypatch):
    from tools.p12_controlled_delivery import run_controlled_delivery

    path = _db(tmp_path, monkeypatch)
    authorization_id = _authorization(path)

    result = run_controlled_delivery(
        lead_id=7, mailbox="zoho", purpose=Purpose.CONTROLLED_TEST,
        confirmation="wrong", transport=lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    assert result.success is False
    assert result.dry_run is True
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT status FROM delivery_authorizations WHERE id=?", (authorization_id,)).fetchone()[0] == AuthorizationStatus.ACTIVE.value
        assert conn.execute("SELECT status FROM leads WHERE id=7").fetchone()[0] == "new"
        assert conn.execute("SELECT COUNT(*) FROM communications WHERE lead_id=7").fetchone()[0] == 0
    finally:
        conn.close()


def test_controlled_command_rejects_unknown_mailbox_without_consuming(tmp_path, monkeypatch):
    from tools.p12_controlled_delivery import run_controlled_delivery

    path = _db(tmp_path, monkeypatch)
    authorization_id = _authorization(path, mailbox="not-a-mailbox")

    result = run_controlled_delivery(
        lead_id=7, mailbox="not-a-mailbox", purpose=Purpose.CONTROLLED_TEST,
        confirmation="SEND-7", transport=lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    assert result.success is False
    assert result.dry_run is True
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT status FROM delivery_authorizations WHERE id=?", (authorization_id,)).fetchone()[0] == AuthorizationStatus.ACTIVE.value
        assert conn.execute("SELECT COUNT(*) FROM communications WHERE lead_id=7").fetchone()[0] == 0
    finally:
        conn.close()


def test_controlled_command_returns_deterministic_failure_when_transport_raises(tmp_path, monkeypatch):
    from tools.p12_controlled_delivery import run_controlled_delivery

    path = _db(tmp_path, monkeypatch)
    authorization_id = _authorization(path)

    result = run_controlled_delivery(
        lead_id=7, mailbox="zoho", purpose=Purpose.CONTROLLED_TEST,
        confirmation="SEND-7", transport=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("fake transport exploded")),
    )

    assert result.success is False
    assert result.dry_run is False
    assert "check authorization status" in result.message
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT status FROM delivery_authorizations WHERE id=?", (authorization_id,)).fetchone()[0] == AuthorizationStatus.CONSUMED.value
        assert conn.execute("SELECT COUNT(*) FROM communications WHERE lead_id=7").fetchone()[0] == 0
    finally:
        conn.close()


def test_controlled_command_reports_post_send_recording_failure(tmp_path, monkeypatch):
    import tools.p12_controlled_delivery as controlled

    path = _db(tmp_path, monkeypatch)
    _authorization(path)
    monkeypatch.setattr(
        controlled, "_record_success",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fake database write failure")),
    )

    result = controlled.run_controlled_delivery(
        lead_id=7, mailbox="zoho", purpose=Purpose.CONTROLLED_TEST,
        confirmation="SEND-7", transport=lambda **kwargs: (True, "sent", "fake-id"),
    )

    assert result.success is True
    assert result.dry_run is False
    assert "manual reconciliation" in result.message
