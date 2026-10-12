"""Protected one-off tool (send_turkey_dealers.py) guard tests (Phase 4).

Every recipient of the tool must pass the SendingGuard before the SMTP
transport. --force bypasses ONLY the daily-quota DELAY; suppression / MX /
domain blocks still hold, and a blocked recipient is skipped without ever
reaching the transport.
"""

import json
import sqlite3

import pytest

from crm.db import build_database


TEST_RECIPIENTS = [
    {"to": "info@dealer-a.example.com", "company": "Example A", "focus": "Example focus"},
    {"to": "info@dealer-b.example.com", "company": "Example B", "focus": "Example focus"},
]


@pytest.fixture(autouse=True)
def private_campaign(tmp_path, monkeypatch):
    from tools import send_turkey_dealers as tool

    path = tmp_path / "campaign.json"
    path.write_text(json.dumps({
        "recipients": TEST_RECIPIENTS,
        "subject": "Example cooperation",
        "body_template": "Dear {company}, {focus} {sig}",
    }), encoding="utf-8")
    monkeypatch.setattr(tool, "DEFAULT_CAMPAIGN_PATH", path)
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    return path


@pytest.fixture
def turkey_db(tmp_path, monkeypatch):
    import config

    db_path = tmp_path / "turkey.db"
    build_database(db_path).close()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    return db_path


def _exhaust_zoho_quota(db_path):
    from emailer.rotation import AntiSpamGuard

    conn = sqlite3.connect(db_path)
    guard = AntiSpamGuard(conn, mailbox="zoho")
    guard.record_send("probe@example.com")  # ensure today's row exists
    conn.execute(
        "UPDATE mailbox_quotas SET email_count = 20 WHERE date = date('now') "
        "AND mailbox = 'zoho'"
    )
    conn.commit()
    conn.close()


def test_tool_suppressed_recipient_never_reaches_smtp(turkey_db, monkeypatch, capsys):
    from services.email_reputation.models import SuppressionReason
    from services.email_reputation.suppression_service import SuppressionService
    from tools.send_turkey_dealers import main

    conn = sqlite3.connect(turkey_db)
    SuppressionService(conn).suppress_domain(
        "dealer-a.example.com",
        reason=SuppressionReason.MANUAL_BLOCK,
        source="test",
    )
    conn.close()

    def forbidden_send(**kwargs):
        raise AssertionError("suppressed tool recipient must never reach SMTP")

    monkeypatch.setattr("emailer.sender.send_email_sync", forbidden_send)

    rc = main(["--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "[SKIP] info@dealer-a.example.com" in out
    assert "suppressed" in out.lower()


def test_tool_daily_quota_skips_without_force(turkey_db, monkeypatch, capsys):
    """Without --force an exhausted quota DELAYs every recipient (never SMTP)."""
    from tools.send_turkey_dealers import main

    _exhaust_zoho_quota(turkey_db)

    def forbidden_send(**kwargs):
        raise AssertionError("daily-capped tool send must never reach SMTP")

    monkeypatch.setattr("emailer.sender.send_email_sync", forbidden_send)

    rc = main(["--dry-run"])
    out = capsys.readouterr().out

    assert rc == 0
    for r in TEST_RECIPIENTS:
        assert f"[SKIP] {r['to']}" in out


def test_tool_force_without_p12_authorization_still_never_reaches_transport(turkey_db, monkeypatch):
    """--force only affects the Guard quota decision; it cannot bypass P12."""
    from services.email_reputation.models import SuppressionReason
    from services.email_reputation.suppression_service import SuppressionService
    from tools.send_turkey_dealers import main

    _exhaust_zoho_quota(turkey_db)
    # One recipient stays suppressed even under --force (only quota is bypassed).
    conn = sqlite3.connect(turkey_db)
    SuppressionService(conn).suppress_domain(
        "dealer-b.example.com",
        reason=SuppressionReason.MANUAL_BLOCK,
        source="test",
    )
    conn.close()
    # The forced path re-runs the MX/domain verdict (only the daily cap is
    # lifted); pin MX so the test is deterministic without real DNS.
    monkeypatch.setattr(
        "utils.email_verify.has_mx_record", lambda domain: True
    )

    sent_to = []

    def ok_send(**kwargs):
        sent_to.append(kwargs["to_email"])
        return True, "sent", "turkey-msg-1"

    monkeypatch.setattr("emailer.sender.send_email_sync", ok_send)

    rc = main(["--force"])
    assert rc == 0
    # Neither recipient can reach SMTP without an exact P12 authorization.
    assert sent_to == []


def test_tool_force_still_blocks_exhausted_domain_quota(
    turkey_db, monkeypatch, capsys
):
    """--force lifts ONLY the daily cap. A recipient whose per-domain quota is
    exhausted must still be BLOCKed (no wire contact, no SMTP failure record)."""
    from tools.send_turkey_dealers import main

    _exhaust_zoho_quota(turkey_db)
    # Exhaust the per-domain tier for BOTH recipients' domains (BUSINESS
    # limit, 15/day) so --force still blocks every send.
    conn = sqlite3.connect(turkey_db)
    conn.execute(
        "UPDATE mailbox_quotas SET domain_counts = "
        "'{\"dealer-a.example.com\": 15, \"dealer-b.example.com\": 15}' "
        "WHERE date = date('now') AND mailbox = 'zoho'"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        "utils.email_verify.has_mx_record", lambda domain: True
    )

    def forbidden_send(**kwargs):
        raise AssertionError("domain-capped tool send must never reach SMTP")

    monkeypatch.setattr("emailer.sender.send_email_sync", forbidden_send)

    rc = main(["--force"])
    out = capsys.readouterr().out

    assert rc == 0
    for r in TEST_RECIPIENTS:
        assert f"[SKIP] {r['to']}" in out
    assert "domain quota is exhausted" in out.lower()


def test_tool_global_dry_run_never_records_quota(turkey_db, monkeypatch, capsys):
    import config
    from tools.send_turkey_dealers import main

    monkeypatch.setattr(config, "EMAIL_DRY_RUN", True, raising=False)
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    monkeypatch.setattr(
        "emailer.sender.send_email_sync",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("global dry-run tool must never reach SMTP")
        ),
    )

    assert main([]) == 0
    output = capsys.readouterr().out
    assert all(f"[GLOBAL DRY-RUN] {r['to']}" in output for r in TEST_RECIPIENTS)
    conn = sqlite3.connect(turkey_db)
    quota = conn.execute(
        "SELECT email_count FROM mailbox_quotas WHERE date = date('now') AND mailbox = 'zoho'"
    ).fetchone()[0]
    conn.close()
    assert quota == 0


def test_missing_campaign_exits_before_database_or_delivery(tmp_path, monkeypatch, capsys):
    from tools import send_turkey_dealers as tool

    monkeypatch.setattr(tool.sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("missing campaign must not open a DB")))
    assert tool.main(["--config", str(tmp_path / "missing.json")]) == 2
    assert "no mail attempted" in capsys.readouterr().err


@pytest.mark.parametrize("campaign", [
    {}, {"recipients": [], "subject": "Example", "body_template": "Example"},
    {"recipients": TEST_RECIPIENTS, "subject": "Example", "body_template": "{unknown}"},
    {"recipients": [{"to": "private-marker", "company": "Example"}],
     "subject": "Example", "body_template": "Example"},
])
def test_invalid_campaign_fails_closed_without_echoing_values(
    tmp_path, monkeypatch, capsys, campaign
):
    from tools import send_turkey_dealers as tool

    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(campaign), encoding="utf-8")
    monkeypatch.setattr(tool.sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("invalid campaign must not open a DB")))
    assert tool.main(["--config", str(path)]) == 2
    output = capsys.readouterr()
    assert "private-marker" not in output.err + output.out


def test_private_campaign_is_loaded_without_changing_content(private_campaign):
    from tools.send_turkey_dealers import _load_campaign

    assert _load_campaign(private_campaign)["recipients"] == TEST_RECIPIENTS
