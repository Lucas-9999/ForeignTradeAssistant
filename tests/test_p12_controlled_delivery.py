"""P12 controlled-delivery integration at the canonical send-core seam.

Every transport below is a fake.  These tests specify the only future path
through P10's lock: Guard ALLOW, P10 unlocked, and one exact authorization.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from crm.db import build_database
from emailer.send_core import SendIntent, send_one, send_one_async
from services.email_reputation.delivery_authorization import (
    AuthorizationStatus,
    DeliveryAuthorizationGate,
    DeliveryAuthorizationRepository,
    Purpose,
)
from services.email_reputation.models import SuppressionReason
from services.email_reputation.sending_guard import build_sending_guard
from services.email_reputation.suppression_service import SuppressionService


def _setup(tmp_path, monkeypatch):
    path = tmp_path / "p12-controlled-delivery.db"
    conn = build_database(path)
    conn.execute(
        "INSERT INTO leads (id, company_name, email, status) "
        "VALUES (7, 'Buyer', 'buyer@example.com', 'new')"
    )
    conn.commit()
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    guard = build_sending_guard(conn, mailbox="zoho")
    now = datetime(2026, 8, 22, 6, 0, tzinfo=timezone.utc)
    repo = DeliveryAuthorizationRepository(conn)
    return conn, guard, repo, now


def _intent(**overrides):
    values = dict(
        email="buyer@example.com",
        subject="P12 controlled delivery test",
        body_html="<p>Fake transport verification only.</p>",
        lead_id=7,
        source="p12_controlled_delivery",
        smtp_profile="zoho",
        purpose=Purpose.CONTROLLED_TEST,
    )
    values.update(overrides)
    return SendIntent(**values)


def _authorize(repo, now):
    return repo.create(
        lead_id=7,
        email="buyer@example.com",
        mailbox="zoho",
        purpose=Purpose.CONTROLLED_TEST,
        expires_at=now + timedelta(minutes=10),
        created_by="test-operator",
        approval_note="fake transport only",
        now=now,
    )


def _gate(repo, now):
    return DeliveryAuthorizationGate(repo, now=lambda: now)


def test_p10_lock_wins_before_valid_authorization_or_transport(tmp_path, monkeypatch):
    import config

    conn, guard, repo, now = _setup(tmp_path, monkeypatch)
    authorization = _authorize(repo, now)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", True, raising=False)
    try:
        outcome = send_one(
            _intent(), sending_guard=guard, delivery_authorization_gate=_gate(repo, now),
            transport=lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not send")),
            now=now,
        )
        assert outcome.dry_run is True
        assert repo.get(authorization.id).status is AuthorizationStatus.ACTIVE
    finally:
        conn.close()


def test_unlocked_process_without_gate_still_simulates_without_transport(tmp_path, monkeypatch):
    import config

    conn, guard, _repo, now = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    try:
        outcome = send_one(
            _intent(), sending_guard=guard,
            transport=lambda **kwargs: (_ for _ in ()).throw(AssertionError("missing gate must not send")),
            now=now,
        )
        assert outcome.dry_run is True
        assert "authorization" in outcome.message.lower()
    finally:
        conn.close()


def test_exact_authorization_is_consumed_once_before_fake_sync_transport(tmp_path, monkeypatch):
    import config

    conn, guard, repo, now = _setup(tmp_path, monkeypatch)
    authorization = _authorize(repo, now)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    calls = []
    try:
        outcome = send_one(
            _intent(), sending_guard=guard, delivery_authorization_gate=_gate(repo, now),
            transport=lambda **kwargs: (calls.append(kwargs) or (True, "sent", "fake-message-id")),
            now=now,
        )
        assert outcome.success is True
        assert outcome.dry_run is False
        assert calls == [{
            "to_email": "buyer@example.com", "subject": "P12 controlled delivery test",
            "html_body": "<p>Fake transport verification only.</p>", "to_name": None,
            "smtp_profile": "zoho",
        }]
        assert repo.get(authorization.id).status is AuthorizationStatus.CONSUMED

        repeat = send_one(
            _intent(), sending_guard=guard, delivery_authorization_gate=_gate(repo, now),
            transport=lambda **kwargs: (_ for _ in ()).throw(AssertionError("consumed authorization must not send")),
            now=now,
        )
        assert repeat.dry_run is True
    finally:
        conn.close()


def test_guard_block_wins_without_consuming_authorization(tmp_path, monkeypatch):
    import config

    conn, guard, repo, now = _setup(tmp_path, monkeypatch)
    authorization = _authorize(repo, now)
    SuppressionService(conn).suppress_email(
        "buyer@example.com", reason=SuppressionReason.MANUAL_BLOCK, source="test"
    )
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    try:
        outcome = send_one(
            _intent(), sending_guard=guard, delivery_authorization_gate=_gate(repo, now),
            transport=lambda **kwargs: (_ for _ in ()).throw(AssertionError("blocked send must not send")),
            now=now,
        )
        assert outcome.blocked is True
        assert outcome.dry_run is False
        assert repo.get(authorization.id).status is AuthorizationStatus.ACTIVE
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_exact_authorization_is_consumed_before_fake_async_transport(tmp_path, monkeypatch):
    import config

    conn, guard, repo, now = _setup(tmp_path, monkeypatch)
    authorization = _authorize(repo, now)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    calls = []

    async def fake_transport(**kwargs):
        calls.append(kwargs)
        return True, "sent", "fake-async-id"

    try:
        outcome = await send_one_async(
            _intent(), sending_guard=guard, delivery_authorization_gate=_gate(repo, now),
            transport=fake_transport, now=now,
        )
        assert outcome.success is True
        assert calls and calls[0]["to_email"] == "buyer@example.com"
        assert repo.get(authorization.id).status is AuthorizationStatus.CONSUMED
    finally:
        conn.close()
