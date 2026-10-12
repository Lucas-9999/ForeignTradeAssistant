"""P11 authorization persistence — no transport or real recipient contact."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from crm.db import build_database
from services.email_reputation.delivery_authorization import (
    AuthorizationDecision,
    AuthorizationStatus,
    DeliveryAuthorizationGate,
    DeliveryAuthorizationRepository,
    Purpose,
)


def _repo(tmp_path):
    path = tmp_path / "authorizations.db"
    conn = build_database(path)
    conn.execute(
        "INSERT INTO leads (id, company_name, email, status) VALUES (7, 'Buyer', 'buyer@example.com', 'new')"
    )
    conn.commit()
    return conn, DeliveryAuthorizationRepository(conn)


def test_create_find_and_consume_exact_authorization(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        authorization = repo.create(
            lead_id=7,
            email="Buyer@Example.com",
            mailbox="default",
            purpose=Purpose.CONTROLLED_TEST,
            expires_at=now + timedelta(minutes=10),
            created_by="operator",
            approval_note="P11 fake-provider verification",
            now=now,
        )

        found = repo.find_active(
            lead_id=7,
            email="buyer@example.com",
            mailbox="default",
            purpose=Purpose.CONTROLLED_TEST,
            now=now,
        )
        assert found is not None and found.id == authorization.id
        assert repo.consume(authorization.id, "attempt-1", now=now) is True
        assert repo.consume(authorization.id, "attempt-2", now=now) is False
        assert repo.get(authorization.id).status is AuthorizationStatus.CONSUMED
    finally:
        conn.close()


def test_expired_scope_is_closed_then_can_be_reauthorized(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        expired = repo.create(
            lead_id=7, email="buyer@example.com", mailbox="default",
            purpose=Purpose.CONTROLLED_TEST, expires_at=now + timedelta(minutes=1),
            created_by="operator", approval_note="short test", now=now,
        )
        replacement = repo.create(
            lead_id=7, email="buyer@example.com", mailbox="default",
            purpose=Purpose.CONTROLLED_TEST, expires_at=now + timedelta(minutes=20),
            created_by="operator", approval_note="replacement", now=now + timedelta(minutes=2),
        )
        assert repo.get(expired.id).status is AuthorizationStatus.EXPIRED
        assert replacement.id != expired.id
    finally:
        conn.close()


def test_list_and_gate_close_expired_records_before_reporting_or_deciding(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        authorization = repo.create(
            lead_id=7, email="buyer@example.com", mailbox="default",
            purpose=Purpose.CONTROLLED_TEST, expires_at=now + timedelta(minutes=1),
            created_by="operator", approval_note="short-lived", now=now,
        )
        listed = repo.list(now=now + timedelta(minutes=2))
        assert listed[0].id == authorization.id
        assert listed[0].status is AuthorizationStatus.EXPIRED
        decision = DeliveryAuthorizationGate(repo, now=lambda: now + timedelta(minutes=2)) \
            .authorize_and_consume(
                lead_id=7, email="buyer@example.com", mailbox="default",
                purpose=Purpose.CONTROLLED_TEST,
            )
        assert decision.decision is AuthorizationDecision.DENY
        assert repo.get(authorization.id).status is AuthorizationStatus.EXPIRED
    finally:
        conn.close()


def test_second_active_scope_is_rejected(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        kwargs = dict(
            lead_id=7, email="buyer@example.com", mailbox="default",
            purpose=Purpose.CONTROLLED_TEST, expires_at=now + timedelta(minutes=10),
            created_by="operator", approval_note="test", now=now,
        )
        repo.create(**kwargs)
        with pytest.raises(ValueError, match="active authorization"):
            repo.create(**kwargs)
    finally:
        conn.close()


def test_gate_denies_missing_purpose_and_consumes_exact_scope_once(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        repo.create(lead_id=7, email="buyer@example.com", mailbox="default",
                    purpose=Purpose.CONTROLLED_TEST, expires_at=now + timedelta(minutes=10),
                    created_by="operator", approval_note="test", now=now)
        gate = DeliveryAuthorizationGate(repo, now=lambda: now)
        missing = gate.authorize_and_consume(lead_id=7, email="buyer@example.com",
                                             mailbox="default", purpose=None)
        allowed = gate.authorize_and_consume(lead_id=7, email="buyer@example.com",
                                             mailbox="default", purpose=Purpose.CONTROLLED_TEST)
        consumed = gate.authorize_and_consume(lead_id=7, email="buyer@example.com",
                                              mailbox="default", purpose=Purpose.CONTROLLED_TEST)
        assert missing.decision is AuthorizationDecision.DENY
        assert missing.reason == "missing_purpose"
        assert allowed.decision is AuthorizationDecision.ALLOW
        assert allowed.authorization_id is not None
        assert consumed.decision is AuthorizationDecision.DENY
        assert consumed.reason == "not_found_or_expired"
    finally:
        conn.close()


def test_revoke_closes_authorization_and_list_is_bounded(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        authorization = repo.create(
            lead_id=7, email="buyer@example.com", mailbox="default",
            purpose=Purpose.MANUAL_VERIFICATION, expires_at=now + timedelta(minutes=10),
            created_by="operator", approval_note="operator changed mind", now=now,
        )
        assert repo.revoke(authorization.id, revoked_by="operator", now=now) is True
        assert repo.revoke(authorization.id, revoked_by="operator", now=now) is False
        revoked = repo.get(authorization.id)
        assert revoked.status is AuthorizationStatus.REVOKED
        assert revoked.closed_by == "operator"
        assert [item.id for item in repo.list(limit=1)] == [authorization.id]
        with pytest.raises(ValueError, match="limit"):
            repo.list(limit=101)
    finally:
        conn.close()


@pytest.mark.parametrize("email", ["not-an-email", "buyer@localhost"])
def test_create_rejects_invalid_email(tmp_path, email):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        with pytest.raises(ValueError, match="email"):
            repo.create(
                lead_id=7, email=email, mailbox="default", purpose=Purpose.CONTROLLED_TEST,
                expires_at=now + timedelta(minutes=10), created_by="operator",
                approval_note="invalid recipient rejection", now=now,
            )
    finally:
        conn.close()


def test_create_rejects_past_expiry_and_database_rejects_invalid_lifecycle_scope(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        with pytest.raises(ValueError, match="future"):
            repo.create(
                lead_id=7, email="buyer@example.com", mailbox="default",
                purpose=Purpose.CONTROLLED_TEST, expires_at=now - timedelta(seconds=1),
                created_by="operator", approval_note="must expire in future", now=now,
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO delivery_authorizations
                   (lead_id,email,mailbox,purpose,expires_at,created_by,created_at,approval_note)
                   VALUES (7, 'buyer@example.com', 'default', 'not-a-purpose',
                           '2026-08-22 12:10:00', 'operator', '2026-08-22 12:00:00', 'test')"""
            )
    finally:
        conn.close()


def test_database_rejects_scope_or_expiry_mutation_after_creation(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        authorization = repo.create(
            lead_id=7, email="buyer@example.com", mailbox="default",
            purpose=Purpose.CONTROLLED_TEST, expires_at=now + timedelta(minutes=10),
            created_by="operator", approval_note="immutable scope", now=now,
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE delivery_authorizations SET email='other@example.com' WHERE id=?",
                (authorization.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE delivery_authorizations SET expires_at='2026-08-22 12:20:00' WHERE id=?",
                (authorization.id,),
            )
    finally:
        conn.close()


def test_cross_connection_consume_allows_exactly_one_winner(tmp_path):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        authorization = repo.create(
            lead_id=7, email="buyer@example.com", mailbox="default",
            purpose=Purpose.CONTROLLED_TEST, expires_at=now + timedelta(minutes=10),
            created_by="operator", approval_note="race test", now=now,
        )
        second_conn = sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2])
        second_conn.row_factory = sqlite3.Row
        try:
            other = DeliveryAuthorizationRepository(second_conn)
            assert repo.consume(authorization.id, "attempt-first", now=now) is True
            assert other.consume(authorization.id, "attempt-second", now=now) is False
        finally:
            second_conn.close()
    finally:
        conn.close()


def test_gate_logs_metadata_only_decisions(tmp_path, caplog):
    conn, repo = _repo(tmp_path)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
    try:
        gate = DeliveryAuthorizationGate(repo, now=lambda: now)
        with caplog.at_level(logging.INFO):
            attempt = gate.authorize_and_consume(
                lead_id=7, email="buyer@example.com", mailbox="default", purpose=None,
            )
        assert attempt.attempt_id
        assert "delivery_authorization_decision" in caplog.text
        assert "subject" not in caplog.text.lower()
        assert "body" not in caplog.text.lower()
        assert "password" not in caplog.text.lower()
    finally:
        conn.close()
