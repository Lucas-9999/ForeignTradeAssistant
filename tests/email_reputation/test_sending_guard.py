"""SendingGuard contract tests (Phase 4).

The guard is a pure decision engine: no SMTP credentials, no sending, no
invented limits from empty health tables. It composes the existing suppression
policy, the existing quota / MX / per-domain tier limits (via
AntiSpamGuard.pre_send_verdict), and only recognizes mailbox-health statuses an
operator already set (paused / restricted / disabled).
"""

import json
import logging
import sqlite3

import pytest

from crm.db import build_database
from services.email_reputation.models import (
    HealthStatus,
    MailboxHealth,
    SuppressionReason,
)
from services.email_reputation.repository import EmailReputationRepository
from services.email_reputation.sending_guard import (
    GuardAction,
    GuardReasonCode,
    SendingGuard,
    StructuredLogGuardRecorder,
    build_sending_guard,
    _next_local_midnight,
)
from services.email_reputation.suppression_service import SuppressionService


def _db(tmp_path, name="guard.db"):
    path = tmp_path / name
    build_database(path).close()
    return path


def _guard(tmp_path, monkeypatch, mailbox="default", **kwargs):
    conn = sqlite3.connect(_db(tmp_path))
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    guard = build_sending_guard(conn, mailbox=mailbox, **kwargs)
    guard._conn = conn  # keep the connection alive for the test body
    return guard


def _paused_health(conn, mailbox, status):
    EmailReputationRepository(conn).upsert_mailbox_health(
        MailboxHealth(
            mailbox=mailbox,
            health_score=10.0,
            status=status,
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


def _exhaust_daily(conn):
    conn.execute(
        "UPDATE sending_quotas SET email_count = 200 WHERE date = date('now')"
    )
    conn.commit()


def _exhaust_domain(conn, domain="example.com"):
    conn.execute(
        "UPDATE sending_quotas SET domain_counts = ? WHERE date = date('now')",
        (json.dumps({domain: 15}),),
    )
    conn.commit()


def _evaluate(guard, email, **ctx):
    return guard.evaluate(
        type("Ctx", (), {"email": email, "mailbox": "default",
                         "override_daily_quota": False, "lead_id": None,
                         "customer_id": None, "campaign": None, "stage": None,
                         "source": None, "requested_time": None, **ctx})()
    )


# ── ALLOW ───────────────────────────────────────────────────────────────


def test_clean_email_allows(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        decision = _evaluate(guard, "buyer@example.com")
        assert decision.action is GuardAction.ALLOW
        assert decision.reason_code is GuardReasonCode.ALLOW
        assert decision.risk_score == 0.0
        assert decision.blocked is False
        assert decision.retry_after is None
    finally:
        guard._conn.close()


# ── Suppression → BLOCK ─────────────────────────────────────────────────


def test_suppressed_email_blocks(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        SuppressionService(guard._conn).suppress_email(
            "buyer@example.com",
            reason=SuppressionReason.UNSUBSCRIBE,
            source="imap",
        )
        decision = _evaluate(guard, "buyer@example.com")
        assert decision.action is GuardAction.BLOCK
        assert decision.reason_code is GuardReasonCode.SUPPRESSED_EMAIL
        assert decision.scope == "email"
        assert decision.risk_score == 1.0
    finally:
        guard._conn.close()


def test_suppressed_domain_blocks(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        SuppressionService(guard._conn).suppress_domain(
            "example.com",
            reason=SuppressionReason.DEAD_DOMAIN,
            source="dns_audit",
        )
        decision = _evaluate(guard, "anyone@example.com")
        assert decision.action is GuardAction.BLOCK
        assert decision.reason_code is GuardReasonCode.SUPPRESSED_DOMAIN
        assert decision.scope == "domain"
    finally:
        guard._conn.close()


def test_legacy_blacklist_blocks(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        guard._conn.execute(
            "INSERT INTO blacklist (email, reason) VALUES (?, ?)",
            ("legacy@example.com", "unsubscribed"),
        )
        guard._conn.commit()
        decision = _evaluate(guard, "legacy@example.com")
        assert decision.action is GuardAction.BLOCK
        assert decision.reason_code is GuardReasonCode.LEGACY_BLACKLIST
    finally:
        guard._conn.close()


def test_invalid_email_blocks(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        decision = _evaluate(guard, "not-an-email")
        assert decision.action is GuardAction.BLOCK
        assert decision.reason_code is GuardReasonCode.INVALID_EMAIL
        assert decision.risk_score == 0.9
    finally:
        guard._conn.close()


def test_empty_email_blocks(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        decision = _evaluate(guard, "  ")
        assert decision.action is GuardAction.BLOCK
        assert decision.reason_code is GuardReasonCode.INVALID_EMAIL
    finally:
        guard._conn.close()


# ── Quota / MX / domain (existing limits, structured) ───────────────────


def test_daily_quota_exhausted_delays(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        _exhaust_daily(guard._conn)
        decision = _evaluate(guard, "buyer@example.com")
        assert decision.action is GuardAction.DELAY
        assert decision.reason_code is GuardReasonCode.DAILY_QUOTA_EXCEEDED
        assert decision.blocked is True
        assert decision.retry_after is not None
    finally:
        guard._conn.close()


def test_daily_quota_override_allows(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        _exhaust_daily(guard._conn)
        decision = _evaluate(
            guard, "buyer@example.com", override_daily_quota=True
        )
        assert decision.action is GuardAction.ALLOW
        assert decision.reason_code is GuardReasonCode.DAILY_QUOTA_OVERRIDDEN
        assert decision.blocked is False
    finally:
        guard._conn.close()


def test_no_mx_blocks(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(
            "utils.email_verify.has_mx_record",
            lambda domain: domain != "no-mx.com",
        )
        decision = _evaluate(guard, "buyer@no-mx.com")
        assert decision.action is GuardAction.BLOCK
        assert decision.reason_code is GuardReasonCode.NO_MX_RECORD
    finally:
        guard._conn.close()


def test_domain_quota_blocks(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        _exhaust_domain(guard._conn, "example.com")
        decision = _evaluate(guard, "buyer@example.com")
        assert decision.action is GuardAction.BLOCK
        assert decision.reason_code is GuardReasonCode.DOMAIN_QUOTA_EXCEEDED
    finally:
        guard._conn.close()


# ── Mailbox health seam: recognize only what an operator set ────────────


def test_empty_health_table_does_not_invent_pause_or_quota(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        # mailbox_health is empty → the decision is driven purely by the real
        # quota/MX/domain state, never by an assumed low health score.
        decision = _evaluate(guard, "buyer@example.com")
        assert decision.action is GuardAction.ALLOW
    finally:
        guard._conn.close()


@pytest.mark.parametrize(
    ("status", "expected_action", "expected_code"),
    [
        (HealthStatus.PAUSED, GuardAction.PAUSE_MAILBOX, GuardReasonCode.MAILBOX_PAUSED),
        (HealthStatus.DISABLED, GuardAction.PAUSE_MAILBOX, GuardReasonCode.MAILBOX_DISABLED),
        (HealthStatus.RESTRICTED, GuardAction.DELAY, GuardReasonCode.MAILBOX_RESTRICTED),
    ],
)
def test_operator_set_health_status_is_recognized(
    tmp_path, monkeypatch, status, expected_action, expected_code
):
    guard = _guard(tmp_path, monkeypatch)
    try:
        _paused_health(guard._conn, "default", status)
        decision = _evaluate(guard, "buyer@example.com")
        assert decision.action is expected_action
        assert decision.reason_code is expected_code
        assert decision.blocked is True
    finally:
        guard._conn.close()


def test_paused_mailbox_has_retry_window_but_disabled_does_not(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        _paused_health(guard._conn, "default", HealthStatus.PAUSED)
        paused = _evaluate(guard, "buyer@example.com")
        assert paused.retry_after is not None

        _paused_health(guard._conn, "default", HealthStatus.DISABLED)
        disabled = _evaluate(guard, "buyer@example.com")
        assert disabled.retry_after is None
    finally:
        guard._conn.close()


def test_healthy_and_warning_statuses_do_not_block(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        for status in (HealthStatus.HEALTHY, HealthStatus.WARNING, HealthStatus.WARMING):
            _paused_health(guard._conn, "default", status)
            decision = _evaluate(guard, "buyer@example.com")
            assert decision.action is GuardAction.ALLOW, status
    finally:
        guard._conn.close()


# ── Serialization / audit: sanitized, no secrets ────────────────────────


def test_decision_serializes_without_secrets_or_diagnostics(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        SuppressionService(guard._conn).suppress_email(
            "buyer@example.com",
            reason=SuppressionReason.HARD_BOUNCE,
            source="smtp:netease",
        )
        decision = _evaluate(guard, "buyer@example.com")
        data = decision.to_dict()
        # Closed, serializable fields only.
        assert set(data) == {
            "action", "reason_code", "message", "risk_score",
            "retry_after", "mailbox", "scope",
        }
        assert data["reason_code"] == "suppressed_email"
        # No SMTP diagnostic, no suppression source/provider, no secret keys.
        blob = json.dumps(data).lower()
        assert "smtp:netease" not in blob
        assert "password" not in blob
        assert "550" not in blob
    finally:
        guard._conn.close()


def test_recorder_emits_structured_sanitized_log(tmp_path, monkeypatch, caplog):
    guard = _guard(tmp_path, monkeypatch)
    try:
        SuppressionService(guard._conn).suppress_email(
            "buyer@example.com",
            reason=SuppressionReason.HARD_BOUNCE,
            source="smtp:netease",
        )
        recorder = StructuredLogGuardRecorder(
            logging.getLogger("test_guard_recorder")
        )
        ctx = guard.evaluate  # noqa
        context = type(
            "Ctx",
            (),
            {
                "email": "buyer@example.com",
                "mailbox": "default",
                "override_daily_quota": False,
                "lead_id": 7,
                "customer_id": None,
                "campaign": None,
                "stage": None,
                "source": "manual",
                "requested_time": "2026-08-20T00:00:00+00:00",
            },
        )()
        decision = _evaluate(guard, "buyer@example.com")
        with caplog.at_level(logging.INFO, logger="test_guard_recorder"):
            recorder.record(context, decision)
        assert len(caplog.records) == 1
        payload = json.loads(caplog.records[0].getMessage().split(" ", 1)[1])
        assert payload["action"] == "BLOCK"
        assert payload["reason_code"] == "suppressed_email"
        assert payload["lead_id"] == 7
        assert payload["email"] == "buyer@example.com"
        assert "smtp:netease" not in json.dumps(payload)
    finally:
        guard._conn.close()


# ── Retry helper ────────────────────────────────────────────────────────


def test_next_local_midnight_is_after_now_and_midnight_aligned(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    now = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)
    value = _next_local_midnight(now)
    parsed = datetime.fromisoformat(value)
    assert parsed > now
    assert parsed.hour == 0
    assert parsed.minute == 0
    assert parsed.second == 0


# ── Consistency with the existing AntiSpamGuard ─────────────────────────
# Shadow-path guarantee: for the state AntiSpamGuard already understands
# (suppression / daily / MX / domain), the guard's ALLOW verdict must match
# can_send's allowed verdict exactly. Health-status decisions are the only
# additions and only fire when an operator set a status.


@pytest.mark.parametrize(
    ("setup", "email"),
    [
        ("clean", "buyer@example.com"),
        ("suppressed", "buyer@example.com"),
        ("daily_exhausted", "buyer@example.com"),
        ("domain_exhausted", "buyer@example.com"),
    ],
)
def test_guard_allows_exactly_when_antispam_allows(
    tmp_path, monkeypatch, setup, email
):
    conn = sqlite3.connect(_db(tmp_path))
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    guard = build_sending_guard(conn)
    try:
        from emailer.rotation import AntiSpamGuard

        if setup == "suppressed":
            SuppressionService(conn).suppress_email(
                email, reason=SuppressionReason.MANUAL_BLOCK, source="test"
            )
        elif setup == "daily_exhausted":
            _exhaust_daily(conn)
        elif setup == "domain_exhausted":
            _exhaust_domain(conn)

        decision = _evaluate(guard, email)
        allowed, _reason = AntiSpamGuard(conn).can_send(email)
        assert decision.blocked is (not allowed)
    finally:
        conn.close()


def test_guard_has_no_send_capability_and_no_credentials(tmp_path, monkeypatch):
    """The guard decides only — it cannot send and never holds a password."""
    guard = _guard(tmp_path, monkeypatch)
    try:
        assert not hasattr(guard, "send")
        assert not hasattr(guard, "deliver")
        assert not hasattr(guard, "password")
        assert not hasattr(guard, "_password")
        assert isinstance(guard, SendingGuard)
    finally:
        guard._conn.close()
