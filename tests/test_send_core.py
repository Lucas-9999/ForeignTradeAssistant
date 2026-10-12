"""Canonical send-core seam tests (Phase 4).

send_core is the single place the SMTP transport is reached. A BLOCK / DELAY /
PAUSE_MAILBOX decision must never touch the wire and must never be reported as
an SMTP delivery failure; only an ALLOW decision reaches the transport, and the
default transport resolves ``emailer.sender`` at call time so existing test
patches keep intercepting it.
"""

import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from crm.db import build_database
from emailer.send_core import (
    EvaluatedSend,
    SendIntent,
    deliver,
    evaluate_send,
    simulate_delivery,
    send_one,
    send_one_async,
)
from services.email_reputation.models import SuppressionReason
from services.email_reputation.delivery_authorization import (
    AuthorizationAttempt,
    AuthorizationDecision,
    Purpose,
)
from services.email_reputation.sending_guard import (
    GuardAction,
    GuardDecision,
    GuardReasonCode,
    build_sending_guard,
)
from services.email_reputation.suppression_service import SuppressionService


def _db(tmp_path):
    path = tmp_path / "send_core.db"
    build_database(path).close()
    return path


def _guard(tmp_path, monkeypatch):
    conn = sqlite3.connect(_db(tmp_path))
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    guard = build_sending_guard(conn)
    guard._conn = conn
    return guard


def _intent(**overrides):
    base = {
        "email": "buyer@example.com",
        "subject": "Hello",
        "body_html": "<p>Hi</p>",
        "to_name": "Buyer",
        "lead_id": 3,
        "source": "manual",
        "smtp_profile": "default",
        "purpose": Purpose.CONTROLLED_TEST,
    }
    base.update(overrides)
    return SendIntent(**base)


class _Recorder:
    def __init__(self):
        self.records = []

    def record(self, context, decision):
        self.records.append((context, decision))


def _ok_transport(to_email, subject, html_body, to_name=None, smtp_profile="default"):
    return True, "sent", "msg-1"


class _PermittingGate:
    """Test seam: production gate persistence is covered in P11/P12 tests."""

    def authorize_and_consume(self, **_kwargs):
        return AuthorizationAttempt(
            decision=AuthorizationDecision.ALLOW,
            authorization_id=1,
            attempt_id="test-attempt",
            reason="authorized_and_consumed",
        )


def _permitting_gate():
    return _PermittingGate()


# ── ALLOW → transport ───────────────────────────────────────────────────


def test_send_one_allows_and_calls_transport(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        calls = []

        def transport(to_email, subject, html_body, to_name=None, smtp_profile="default"):
            calls.append((to_email, subject, html_body, to_name, smtp_profile))
            return True, "sent", "m1"

        outcome = send_one(
            _intent(), sending_guard=guard, transport=transport,
            delivery_authorization_gate=_permitting_gate(),
        )
        assert outcome.success is True
        assert outcome.guarded is True
        assert outcome.blocked is False
        assert outcome.message_id == "m1"
        assert calls == [("buyer@example.com", "Hello", "<p>Hi</p>", "Buyer", "default")]
    finally:
        guard._conn.close()


def test_global_dry_run_returns_auditable_outcome_without_transport(tmp_path, monkeypatch):
    """P10 is fail-closed: an ALLOW decision may be simulated, never sent."""
    import config

    guard = _guard(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", True, raising=False)
    try:
        def forbidden_transport(*args, **kwargs):
            raise AssertionError("global dry-run must never reach SMTP")

        outcome = send_one(
            _intent(), sending_guard=guard, transport=forbidden_transport
        )

        assert outcome.success is False
        assert outcome.dry_run is True
        assert outcome.blocked is False
        assert outcome.message_id is None
        assert "dry-run" in outcome.message.lower()
    finally:
        guard._conn.close()


def test_explicit_simulation_outcome_never_needs_a_transport(tmp_path, monkeypatch):
    """Two-phase callers can audit P10 simulation before making any DB claim."""
    guard = _guard(tmp_path, monkeypatch)
    try:
        evaluated = evaluate_send(_intent(), sending_guard=guard)

        outcome = simulate_delivery(evaluated)

        assert outcome.dry_run is True
        assert outcome.success is False
        assert outcome.blocked is False
    finally:
        guard._conn.close()


def test_production_process_defaults_to_dry_run_until_operator_enables_outbound():
    """Normal delivery stays dry-run unless the operator setting is true."""
    env = dict(os.environ)
    # Explicitly override a developer's local .env so this subprocess never
    # inherits a real production opt-in during the test suite.
    env["FTA_OUTBOUND_DELIVERY_ENABLED"] = "false"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.EMAIL_DRY_RUN)",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True"


def test_production_process_allows_delivery_only_with_explicit_operator_enablement():
    """P13 turns off simulation only through its named operator control."""
    env = dict(os.environ)
    env["FTA_OUTBOUND_DELIVERY_ENABLED"] = "true"
    env["FTA_IMAP_CHECK_ENABLED"] = "true"
    env["FTA_IMAP_USERNAME"] = "monitor@example.com"
    env["FTA_IMAP_PASSWORD"] = "test-only-password"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.EMAIL_DRY_RUN, config.OUTBOUND_DELIVERY_ENABLED)",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False True"


def test_production_process_keeps_dry_run_when_bounce_monitoring_is_unavailable():
    """P13 cannot enable normal delivery without its asynchronous DSN path."""
    env = dict(os.environ)
    env["FTA_OUTBOUND_DELIVERY_ENABLED"] = "true"
    env["FTA_IMAP_CHECK_ENABLED"] = "false"
    env.pop("FTA_IMAP_USERNAME", None)
    env.pop("FTA_IMAP_PASSWORD", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.EMAIL_DRY_RUN, config.OUTBOUND_DELIVERY_ENABLED)",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "True False"


def test_operator_enabled_normal_delivery_does_not_require_one_time_authorization(
    tmp_path, monkeypatch
):
    """P13 normal operations retain SendingGuard but replace P12's test-only gate."""
    import config

    guard = _guard(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    monkeypatch.setattr(config, "OUTBOUND_DELIVERY_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "emailer.send_core.normal_outbound_delivery_ready",
        lambda profile: (True, f"{profile} IMAP monitor ready"),
    )
    try:
        outcome = send_one(
            _intent(purpose=None),
            sending_guard=guard,
            transport=_ok_transport,
        )

        assert outcome.success is True
        assert outcome.dry_run is False
    finally:
        guard._conn.close()


def test_operator_enabled_zoho_delivery_stays_simulated_until_zoho_dsn_loop_exists(
    tmp_path, monkeypatch
):
    """P13's normal-production scope is 163 only, not manual Zoho mail."""
    import config

    guard = _guard(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    monkeypatch.setattr(config, "OUTBOUND_DELIVERY_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "emailer.send_core.normal_outbound_delivery_ready",
        lambda _profile: (_ for _ in ()).throw(
            AssertionError("Zoho must not reach IMAP readiness in P13")
        ),
    )
    try:
        outcome = send_one(
            _intent(smtp_profile="zoho", purpose=None),
            sending_guard=guard,
            transport=_ok_transport,
        )

        assert outcome.success is False
        assert outcome.dry_run is True
        assert "default" in outcome.message
    finally:
        guard._conn.close()


def test_operator_enabled_normal_delivery_fails_closed_when_imap_monitor_is_unreachable(
    tmp_path, monkeypatch
):
    """P13 never starts SMTP when asynchronous bounce detection is unavailable."""
    import config

    guard = _guard(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    monkeypatch.setattr(config, "OUTBOUND_DELIVERY_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "emailer.send_core.normal_outbound_delivery_ready",
        lambda _profile: (False, "IMAP monitor connection failed"),
    )
    try:
        outcome = send_one(
            _intent(purpose=None),
            sending_guard=guard,
            transport=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("unreachable IMAP monitor must block SMTP")
            ),
        )

        assert outcome.success is False
        assert outcome.dry_run is True
        assert "IMAP monitor" in outcome.message
    finally:
        guard._conn.close()


def test_global_dry_run_handles_100_allowed_decisions_without_smtp(tmp_path, monkeypatch):
    """P10 soak proof: 100 Guard ALLOWs produce outcomes, not wire traffic."""
    import config

    guard = _guard(tmp_path, monkeypatch)
    recorder = _Recorder()
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", True, raising=False)
    try:
        def forbidden_transport(*args, **kwargs):
            raise AssertionError("P10 decision soak must never reach SMTP")

        outcomes = [
            send_one(
                _intent(email=f"buyer{index}@example.com", lead_id=index),
                sending_guard=guard,
                recorder=recorder,
                transport=forbidden_transport,
            )
            for index in range(100)
        ]

        assert len(recorder.records) == 100
        assert all(outcome.dry_run and not outcome.success for outcome in outcomes)
    finally:
        guard._conn.close()


def test_global_dry_run_audit_excludes_message_content(tmp_path, monkeypatch, caplog):
    import config

    guard = _guard(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", True, raising=False)
    caplog.set_level("INFO", logger="emailer.send_core")
    try:
        send_one(
            _intent(subject="confidential subject", body_html="secret message body"),
            sending_guard=guard,
            transport=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("dry-run must never reach SMTP")
            ),
        )

        record = next(
            item.message for item in caplog.records if "email_global_dry_run" in item.message
        )
        assert '"source": "manual"' in record
        assert "confidential subject" not in record
        assert "secret message body" not in record
    finally:
        guard._conn.close()


def test_self_hosted_flag_cannot_change_the_default_transport(tmp_path, monkeypatch):
    """P9 readiness may be enabled, but it cannot migrate live mail traffic."""
    import config
    from emailer import sender

    guard = _guard(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(config, "SELF_HOSTED_SMTP_ENABLED", True, raising=False)
    monkeypatch.setattr(
        sender,
        "send_email_sync",
        lambda **kwargs: (calls.append(kwargs) or (True, "sent", "p9-msg")),
    )
    try:
        outcome = send_one(
            _intent(), sending_guard=guard,
            delivery_authorization_gate=_permitting_gate(),
        )

        assert outcome.success is True
        assert outcome.message_id == "p9-msg"
        assert calls and calls[0]["smtp_profile"] == "default"
    finally:
        guard._conn.close()


# ── BLOCK → no transport, no SMTP failure semantics ─────────────────────


def test_send_one_blocked_never_calls_transport(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        SuppressionService(guard._conn).suppress_email(
            "buyer@example.com",
            reason=SuppressionReason.MANUAL_BLOCK,
            source="test",
        )

        def forbidden_transport(*args, **kwargs):
            raise AssertionError("blocked send must never reach SMTP")

        outcome = send_one(
            _intent(), sending_guard=guard, transport=forbidden_transport
        )
        assert outcome.success is False
        assert outcome.blocked is True
        assert outcome.guarded is True
        assert outcome.decision.action is GuardAction.BLOCK
        assert outcome.message == outcome.decision.message
    finally:
        guard._conn.close()


def test_deliver_rejects_a_forged_allow_without_reaching_smtp():
    """Transport requires the private capability created by evaluate_send()."""
    forged = GuardDecision(
        action=GuardAction.ALLOW,
        reason_code=GuardReasonCode.ALLOW,
        message="forged",
    )
    def forbidden_transport(*args, **kwargs):
        raise AssertionError("forged ALLOW must never reach SMTP")

    with pytest.raises(TypeError, match="EvaluatedSend"):
        deliver(forged, transport=forbidden_transport)  # type: ignore[arg-type]

    fake_capability = EvaluatedSend(
        intent=_intent(), decision=forged, _capability=object()
    )
    with pytest.raises(TypeError, match="EvaluatedSend"):
        deliver(fake_capability, transport=forbidden_transport)


# ── Async path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_one_async_allows_and_calls_async_transport(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        calls = []

        async def transport(to_email, subject, html_body, to_name=None):
            calls.append((to_email, subject, html_body, to_name))
            return True, "sent", "a1"

        outcome = await send_one_async(
            _intent(), sending_guard=guard, transport=transport,
            delivery_authorization_gate=_permitting_gate(),
        )
        assert outcome.success is True
        assert calls == [("buyer@example.com", "Hello", "<p>Hi</p>", "Buyer")]
    finally:
        guard._conn.close()


@pytest.mark.asyncio
async def test_global_dry_run_stops_async_transport(tmp_path, monkeypatch):
    import config

    guard = _guard(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", True, raising=False)
    try:
        async def forbidden_transport(*args, **kwargs):
            raise AssertionError("global dry-run must never reach async SMTP")

        outcome = await send_one_async(
            _intent(), sending_guard=guard, transport=forbidden_transport
        )

        assert outcome.dry_run is True
        assert outcome.success is False
        assert outcome.blocked is False
    finally:
        guard._conn.close()


@pytest.mark.asyncio
async def test_send_one_async_blocked_never_calls_transport(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        SuppressionService(guard._conn).suppress_email(
            "buyer@example.com",
            reason=SuppressionReason.MANUAL_BLOCK,
            source="test",
        )

        async def forbidden_transport(*args, **kwargs):
            raise AssertionError("blocked send must never reach SMTP")

        outcome = await send_one_async(
            _intent(), sending_guard=guard, transport=forbidden_transport
        )
        assert outcome.blocked is True
        assert outcome.success is False
    finally:
        guard._conn.close()


# ── Two-phase API (batch claims a lead between decision and transport) ───


def test_evaluate_send_records_once_then_deliver(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    recorder = _Recorder()
    try:
        intent = _intent()
        evaluated = evaluate_send(
            intent, sending_guard=guard, recorder=recorder
        )
        assert isinstance(evaluated, EvaluatedSend)
        decision = evaluated.decision
        assert decision.action is GuardAction.ALLOW
        assert len(recorder.records) == 1
        recorded_ctx, recorded_decision = recorder.records[0]
        assert recorded_ctx.email == "buyer@example.com"
        assert recorded_ctx.lead_id == 3
        assert recorded_decision.action is GuardAction.ALLOW

        outcome = deliver(
            evaluated, transport=_ok_transport,
            delivery_authorization_gate=_permitting_gate(),
        )
        assert outcome.success is True
        # deliver must not re-record.
        assert len(recorder.records) == 1
    finally:
        guard._conn.close()


def test_override_daily_quota_propagates_to_guard(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        guard._conn.execute(
            "UPDATE sending_quotas SET email_count = 200 WHERE date = date('now')"
        )
        guard._conn.commit()

        outcome = send_one(
            _intent(override_daily_quota=True),
            sending_guard=guard,
            transport=_ok_transport,
            delivery_authorization_gate=_permitting_gate(),
        )
        assert outcome.success is True
        assert outcome.decision.reason_code is GuardReasonCode.DAILY_QUOTA_OVERRIDDEN
    finally:
        guard._conn.close()


def test_override_still_enforces_mx(tmp_path, monkeypatch):
    """The operator daily-quota override lifts ONLY the daily cap. A recipient
    domain without MX must still be BLOCKed — never allowed, never sent."""
    guard = _guard(tmp_path, monkeypatch)
    try:
        guard._conn.execute(
            "UPDATE sending_quotas SET email_count = 200 WHERE date = date('now')"
        )
        guard._conn.commit()
        monkeypatch.setattr(
            "utils.email_verify.has_mx_record", lambda domain: False
        )

        def forbidden_transport(*args, **kwargs):
            raise AssertionError("no-MX override send must never reach SMTP")

        outcome = send_one(
            _intent(override_daily_quota=True),
            sending_guard=guard,
            transport=forbidden_transport,
        )
        assert outcome.success is False
        assert outcome.blocked is True
        assert outcome.decision.action is GuardAction.BLOCK
        assert outcome.decision.reason_code is GuardReasonCode.NO_MX_RECORD
    finally:
        guard._conn.close()


def test_override_still_enforces_domain_quota(tmp_path, monkeypatch):
    """The override must also still enforce the per-domain tier: an exhausted
    domain stays BLOCKed under override_daily_quota."""
    guard = _guard(tmp_path, monkeypatch)
    try:
        guard._conn.execute(
            "UPDATE sending_quotas SET email_count = 200, domain_counts = "
            "'{\"example.com\": 15}' WHERE date = date('now')"
        )
        guard._conn.commit()

        def forbidden_transport(*args, **kwargs):
            raise AssertionError("domain-capped override send must never reach SMTP")

        outcome = send_one(
            _intent(override_daily_quota=True),
            sending_guard=guard,
            transport=forbidden_transport,
        )
        assert outcome.success is False
        assert outcome.blocked is True
        assert outcome.decision.reason_code is GuardReasonCode.DOMAIN_QUOTA_EXCEEDED
    finally:
        guard._conn.close()


# ── Default transport resolves emailer.sender at call time ──────────────


def test_default_sync_transport_hits_patched_emailer_sender(tmp_path, monkeypatch):
    """Existing tests monkeypatch ``emailer.sender.send_email_sync``; the
    canonical seam must resolve it through the module at call time."""
    guard = _guard(tmp_path, monkeypatch)
    try:
        calls = []

        def fake_sync(to_email, subject, html_body, **kwargs):
            calls.append((to_email, kwargs.get("smtp_profile")))
            return True, "sent", "mid"

        monkeypatch.setattr("emailer.sender.send_email_sync", fake_sync)

        outcome = send_one(
            _intent(smtp_profile="zoho"), sending_guard=guard,
            delivery_authorization_gate=_permitting_gate(),
        )
        assert outcome.success is True
        assert outcome.message_id == "mid"
        assert calls == [("buyer@example.com", "zoho")]
    finally:
        guard._conn.close()


@pytest.mark.asyncio
async def test_default_async_transport_hits_patched_emailer_sender(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        calls = []

        async def fake_async(to_email, subject, html_body, **kwargs):
            calls.append(to_email)
            return True, "sent", "mid-async"

        monkeypatch.setattr("emailer.sender.send_email", fake_async)

        outcome = await send_one_async(
            _intent(), sending_guard=guard,
            delivery_authorization_gate=_permitting_gate(),
        )
        assert outcome.success is True
        assert calls == ["buyer@example.com"]
    finally:
        guard._conn.close()


# ── Non-bypassability: the transport is only reachable through send_core ─


def test_transport_only_reached_through_send_core():
    """Structural proof (P4 §5): no production module may reference the SMTP
    transport except ``send_core`` (the canonical seam) and ``sender`` (the
    transport itself). Any future entry point that tries to send without a
    SendingGuard decision will trip this test.

    Scans every Python source root for direct imports/calls of the transport
    functions; only the seam itself (and ``sender``, the transport) may mention
    them. ``app.py`` and the ``api`` / ``services`` / ``collector`` roots are
    included so a bypass added anywhere in the repo trips this test.
    """
    import pathlib
    import re

    roots = ["app.py", "api", "crm", "collector", "emailer", "engine",
             "services", "tools", "ui", "utils"]
    pattern = re.compile(
        r"(from emailer\.sender import "
        r"|import send_email\b"
        r"|send_email_sync\s*\("
        r"|send_email\s*\(\s*to_email"
        r"|sender\.send_email\b"
        r"|\.sendmail\("
        r"|\.send_message\()"
    )
    offenders: list[str] = []
    for root in roots:
        root_path = pathlib.Path(root)
        paths = [root_path] if root_path.is_file() else root_path.rglob("*.py")
        for path in paths:
            if path.name in ("send_core.py", "sender.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path}:{lineno}: {line.strip()}")

    assert offenders == [], (
        "SMTP transport referenced outside the canonical seam:\n"
        + "\n".join(offenders)
    )


# ── Decision ordering: suppression wins over quota ──────────────────────


def test_suppression_wins_over_quota(tmp_path, monkeypatch):
    guard = _guard(tmp_path, monkeypatch)
    try:
        SuppressionService(guard._conn).suppress_domain(
            "example.com",
            reason=SuppressionReason.DEAD_DOMAIN,
            source="dns_audit",
        )
        outcome = send_one(
            _intent(), sending_guard=guard, transport=_ok_transport
        )
        assert outcome.blocked is True
        assert outcome.decision.reason_code is GuardReasonCode.SUPPRESSED_DOMAIN
    finally:
        guard._conn.close()
