"""
Tests for email module — template rendering, anti-spam logic.
"""

import pytest

from emailer.templates import EmailTemplateRenderer
from emailer.rotation import AntiSpamGuard


class TestEmailTemplateRenderer:
    """Tests for Jinja2 email template rendering."""

    def setup_method(self):
        self.renderer = EmailTemplateRenderer(sender_company="Test Fastener Co.")

    def test_render_subject(self):
        result = self.renderer.render_subject(
            "Hello {{ company_name }}",
            company_name="ACME Corp",
        )
        assert result == "Hello ACME Corp"

    def test_render_subject_with_default(self):
        """None contact_name → code-level fallback 'Valued Customer'."""
        result = self.renderer.render_subject(
            "Hi {{ contact_name }}",
            contact_name=None,
        )
        assert result == "Hi Valued Customer"

    def test_render_subject_with_empty_contact(self):
        """Empty string contact_name → code-level fallback."""
        result = self.renderer.render_subject(
            "Dear {{ contact_name }},",
            contact_name="",
        )
        assert result == "Dear Valued Customer,"

    def test_render_body(self):
        body = "<html><body><p>Dear {{ contact_name }},</p><p>{{ company_name }}</p></body></html>"
        result = self.renderer.render_body(
            body,
            company_name="ACME Corp",
            contact_name="John",
        )
        assert "Dear John" in result
        assert "ACME Corp" in result

    def test_render_body_fallback_on_error(self):
        """Template with missing variable should still return something."""
        result = self.renderer.render_body(
            "<p>{{ undefined_var }}</p>",
            company_name="Test",
        )
        # Should not raise — returns fallback or rendered with empty
        assert result is not None

    def test_preview(self):
        result = self.renderer.preview(
            subject_template="Question about {{ company_name }}",
            body_template="<p>Hi {{ contact_name }}</p>",
        )
        assert "subject" in result
        assert "body" in result
        assert "ACME Corp" in result["subject"]
        assert "John Smith" in result["body"]

    def test_preview_with_custom_names(self):
        result = self.renderer.preview(
            subject_template="{{ company_name }} — Fastener Quote",
            body_template="<p>Hello from {{ sender_name }}</p>",
            company_name="Custom Corp",
            contact_name="Jane Doe",
        )
        assert "Custom Corp" in result["subject"]
        assert "Foreign Trade Team" in result["body"]  # sender_name from config


class TestAntiSpamGuard:
    """Tests for anti-spam rate limiting."""

    def test_can_send_clean(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        ok, reason = guard.can_send("new@example.com")
        assert ok
        assert reason == ""

    def test_blacklisted_email(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        guard._add_to_blacklist("blocked@test.com", "bounced")

        ok, reason = guard.can_send("blocked@test.com")
        assert not ok
        assert "blacklisted" in reason.lower()

    def test_unified_domain_suppression_blocks_before_send(self, db_conn):
        from services.email_reputation.models import SuppressionReason
        from services.email_reputation.suppression_service import SuppressionService

        SuppressionService(db_conn).suppress_domain(
            "example.com",
            reason=SuppressionReason.DEAD_DOMAIN,
            source="dns_audit",
        )
        guard = AntiSpamGuard(db_conn)

        ok, reason = guard.can_send("buyer@example.com")

        assert ok is False
        assert "suppressed" in reason.lower()
        assert "dead_domain" in reason
        assert "dns_audit" not in reason

    def test_record_send_updates_quota(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        guard.record_send("fixture.sender09@gmail.com")

        status = guard.get_status()
        assert status["emails_sent_today"] == 1
        assert status["domain_counts"].get("gmail.com", 0) == 1

    def test_domain_counting(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        guard.record_send("fixture.sender01@gmail.com")
        guard.record_send("fixture.sender02@gmail.com")
        guard.record_send("x@yahoo.com")

        status = guard.get_status()
        assert status["domain_counts"]["gmail.com"] == 2
        assert status["domain_counts"]["yahoo.com"] == 1

    def test_can_send_batch(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        guard._add_to_blacklist("bad@test.com", "bounced")

        ok, reason, blocked = guard.can_send_batch(
            ["good@example.com", "bad@test.com", "also-good@domain.com"]
        )
        assert not ok
        assert len(blocked) == 1
        assert "bad@test.com" in blocked[0]

    def test_record_bounce(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        guard.record_bounce("bounced@test.com")

        ok, _ = guard.can_send("bounced@test.com")
        assert not ok  # should be blacklisted

    def test_confirmed_smtp_hard_bounce_enters_unified_suppression(self, db_conn):
        guard = AntiSpamGuard(db_conn)

        suppressed = guard.record_smtp_failure(
            "bounced@test.com",
            "550 5.1.1 user unknown",
            provider="netease",
        )

        row = db_conn.execute(
            "SELECT reason, source FROM email_suppressions WHERE email = ?",
            ("bounced@test.com",),
        ).fetchone()
        assert suppressed is True
        assert tuple(row) == ("hard_bounce", "smtp:netease")

    def test_hard_bounce_persistence_failure_installs_legacy_fail_closed_fallback(
        self, db_conn, monkeypatch
    ):
        import sqlite3

        guard = AntiSpamGuard(db_conn)

        def fail_canonical_write(*args, **kwargs):
            raise sqlite3.OperationalError("simulated canonical write failure")

        monkeypatch.setattr(
            guard.suppression_service,
            "suppress_email",
            fail_canonical_write,
        )

        suppressed = guard.record_smtp_failure(
            "bounced@test.com",
            "550 5.1.1 user unknown",
            provider="netease",
        )

        legacy = db_conn.execute(
            "SELECT reason FROM blacklist WHERE email = ?",
            ("bounced@test.com",),
        ).fetchone()
        assert suppressed is True
        assert legacy[0] == "hard_bounce_fallback"
        assert guard.can_send("bounced@test.com")[0] is False

    def test_quota_status(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        status = guard.get_status()

        assert "emails_sent_today" in status
        assert "daily_limit" in status
        assert "remaining" in status
        assert "domain_counts" in status
        assert status["emails_sent_today"] == 0


def test_smtp_exception_is_sanitized_before_leaving_transport(monkeypatch):
    import smtplib
    from email.mime.text import MIMEText

    from emailer.sender import _send_sync

    class FailingSmtp:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, username, password):
            raise smtplib.SMTPException(
                "550 5.1.1 user unknown buyer@example.com password=hunter2"
            )

    monkeypatch.setattr("emailer.sender.smtplib.SMTP_SSL", FailingSmtp)

    ok, message = _send_sync(
        MIMEText("test"),
        "buyer@example.com",
        "sender@example.com",
        "smtp-secret",
        "smtp.invalid",
        465,
        False,
    )

    assert ok is False
    assert "buyer@example.com" not in message
    assert "hunter2" not in message
    assert "[EMAIL_REDACTED]" in message
    assert "[REDACTED]" in message


def test_guarded_one_off_tool_without_p12_authorization_does_not_reach_smtp(
    tmp_path, monkeypatch
):
    import sqlite3

    import config
    from crm.db import build_database
    from tools import send_turkey_dealers

    db_path = tmp_path / "tool.db"
    build_database(db_path).close()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(send_turkey_dealers, "_force_utf8", lambda: None)
    monkeypatch.setattr(
        send_turkey_dealers,
        "_load_campaign",
        lambda path: {
            "recipients": [{"to": "gone@example.com", "company": "Gone", "focus": "test"}],
            "subject": "Example",
            "body_template": "Dear {company}, {focus} {sig}",
        },
    )
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    monkeypatch.setattr(
        "emailer.sender.send_email_sync",
        lambda *args, **kwargs: (
            False,
            "HARD_BOUNCE: 550 5.1.1 user unknown",
            None,
        ),
    )

    exit_code = send_turkey_dealers.main([])

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT reason, source FROM email_suppressions").fetchone()
    conn.close()
    assert exit_code == 0
    assert row is None


def _build_followup_candidate(tmp_path, filename, email):
    import sqlite3

    from crm.db import build_database

    db_path = tmp_path / filename
    build_database(db_path).close()
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """INSERT INTO leads (company_name, email, status)
           VALUES ('Follow-up Co', ?, 'contacted')""",
        (email,),
    )
    conn.execute(
        """INSERT INTO communications
           (lead_id, template_id, channel, direction, status, sent_at)
           VALUES (?, 'general_01', 'email', 'outbound', 'sent',
                   datetime('now', '-4 days'))""",
        (cursor.lastrowid,),
    )
    conn.commit()
    conn.close()
    return db_path


def test_followup_stage_cannot_bypass_unified_suppression(tmp_path, monkeypatch):
    import sqlite3

    from engine.followup import FollowUpEngine
    from services.email_reputation.models import SuppressionReason
    from services.email_reputation.suppression_service import SuppressionService

    db_path = _build_followup_candidate(
        tmp_path, "followup.db", "buyer@example.com"
    )
    conn = sqlite3.connect(db_path)
    SuppressionService(conn).suppress_domain(
        "example.com",
        reason=SuppressionReason.DEAD_DOMAIN,
        source="dns_audit",
    )
    conn.close()

    def forbidden_send(**kwargs):
        raise AssertionError("suppressed follow-up must never reach SMTP")

    monkeypatch.setattr(
        "emailer.sender.send_email_sync",
        forbidden_send,
    )
    monkeypatch.setattr("emailer.rotation.compute_send_delay", lambda: 0)

    result = FollowUpEngine(db_path).run_stage(1)

    assert result.candidates_found == 1
    assert result.skipped == 1
    assert result.sent == 0
    assert result.details[0]["status"].startswith("blocked:")


def test_global_dry_run_skips_followup_without_recording_delivery(tmp_path, monkeypatch):
    """P10 simulation must not create a follow-up communication or quota use."""
    import sqlite3
    import config

    from engine.followup import FollowUpEngine

    db_path = _build_followup_candidate(
        tmp_path, "followup-global-dry-run.db", "preview@example.com"
    )
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", True, raising=False)
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    monkeypatch.setattr(
        "emailer.rotation.compute_send_delay",
        lambda: (_ for _ in ()).throw(
            AssertionError("global dry-run follow-up must not wait between sends")
        ),
    )
    monkeypatch.setattr(
        "emailer.sender.send_email_sync",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("global dry-run follow-up must never reach SMTP")
        ),
    )

    result = FollowUpEngine(db_path).run_stage(1)

    assert result.sent == 0
    assert result.skipped == 1
    assert result.errors == 0
    assert result.details[0]["status"] == "global_dry_run"
    conn = sqlite3.connect(db_path)
    comms = conn.execute(
        "SELECT COUNT(*) FROM communications WHERE template_id = 'followup_01'"
    ).fetchone()[0]
    quota = conn.execute(
        "SELECT email_count FROM sending_quotas WHERE date = date('now')"
    ).fetchone()[0]
    conn.close()
    assert comms == 0
    assert quota == 0


def test_followup_without_p12_authorization_does_not_reach_smtp(tmp_path, monkeypatch):
    import sqlite3

    from engine.followup import FollowUpEngine

    db_path = _build_followup_candidate(
        tmp_path, "followup-hard.db", "gone@example.com"
    )
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
    monkeypatch.setattr(
        "emailer.sender.send_email_sync",
        lambda **kwargs: (False, "HARD_BOUNCE: 550 5.1.1 user unknown", None),
    )
    monkeypatch.setattr("emailer.rotation.compute_send_delay", lambda: 0)

    result = FollowUpEngine(db_path).run_stage(1)

    conn = sqlite3.connect(db_path)
    suppression = conn.execute(
        "SELECT reason, source FROM email_suppressions"
    ).fetchone()
    conn.close()
    assert result.errors == 0
    assert result.skipped == 1
    assert suppression is None


def test_followup_daily_cap_blocks_without_smtp(tmp_path, monkeypatch):
    """The SendingGuard (not just suppression) gates follow-ups: a daily-cap
    DELAY must stop the send before the transport and must not be recorded as a
    communication or SMTP failure."""
    import sqlite3
    from datetime import date

    from engine.followup import FollowUpEngine

    db_path = _build_followup_candidate(
        tmp_path, "followup-cap.db", "buyer@example.com"
    )
    # Seed today's quota at the cap — AntiSpamGuard's constructor only
    # INSERT OR IGNOREs a fresh 0-row when none exists.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sending_quotas (date, email_count, domain_counts) "
        "VALUES (?, 200, '{}')",
        (date.today().isoformat(),),
    )
    conn.commit()
    conn.close()

    def forbidden_send(**kwargs):
        raise AssertionError("daily-capped follow-up must never reach SMTP")

    monkeypatch.setattr("emailer.sender.send_email_sync", forbidden_send)
    monkeypatch.setattr("emailer.rotation.compute_send_delay", lambda: 0)

    result = FollowUpEngine(db_path).run_stage(1)

    assert result.candidates_found == 1
    assert result.skipped == 1
    assert result.sent == 0
    assert result.details[0]["status"].startswith("blocked:")
    # Blocked attempt is not recorded as a communication.
    conn = sqlite3.connect(db_path)
    comms = conn.execute(
        "SELECT COUNT(*) FROM communications WHERE template_id = 'followup_01'"
    ).fetchone()[0]
    conn.close()
    assert comms == 0


def test_followup_paused_mailbox_halts_without_smtp(tmp_path, monkeypatch):
    """A PAUSED mailbox on the health seam halts every follow-up send."""
    import sqlite3

    from engine.followup import FollowUpEngine
    from services.email_reputation.models import HealthStatus, MailboxHealth
    from services.email_reputation.repository import EmailReputationRepository

    db_path = _build_followup_candidate(
        tmp_path, "followup-pause.db", "buyer@example.com"
    )
    conn = sqlite3.connect(db_path)
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

    def forbidden_send(**kwargs):
        raise AssertionError("paused-mailbox follow-up must never reach SMTP")

    monkeypatch.setattr("emailer.sender.send_email_sync", forbidden_send)
    monkeypatch.setattr("emailer.rotation.compute_send_delay", lambda: 0)

    result = FollowUpEngine(db_path).run_stage(1)

    assert result.skipped == 1
    assert result.sent == 0
    assert result.details[0]["status"].startswith("blocked:")


def test_streamlit_single_send_without_p12_authorization_does_not_reach_smtp(tmp_path, monkeypatch):
    import sqlite3

    from crm.db import build_database
    from ui import email_send

    db_path = tmp_path / "ui-send.db"
    conn = build_database(db_path)
    conn.execute(
        """INSERT INTO leads (company_name, email, status)
           VALUES ('UI Co', 'gone@example.com', 'new')"""
    )
    conn.execute(
        """INSERT INTO email_templates
           (name, subject_template, body_template, category, is_active)
           VALUES ('UI Test', 'Hello', '<p>Hello</p>', 'icebreaker', 1)"""
    )
    conn.commit()

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(email_send.st, "session_state", {})
    monkeypatch.setattr(email_send.st, "tabs", lambda labels: [Context()] * 3)
    monkeypatch.setattr(email_send.st, "columns", lambda count: [Context()] * count)
    monkeypatch.setattr(email_send.st, "expander", lambda *a, **k: Context())
    monkeypatch.setattr(email_send.st, "spinner", lambda *a, **k: Context())
    monkeypatch.setattr(email_send.st, "selectbox", lambda label, options, **k: options[0])
    monkeypatch.setattr(email_send.st, "text_input", lambda label, value, **k: value)
    monkeypatch.setattr(email_send.st, "text_area", lambda label, value, **k: value)
    monkeypatch.setattr(email_send.st, "checkbox", lambda *a, **k: False)
    monkeypatch.setattr(email_send.st, "button", lambda *a, **k: True)
    for name in (
        "title",
        "metric",
        "divider",
        "subheader",
        "markdown",
        "html",
        "balloons",
    ):
        monkeypatch.setattr(email_send.st, name, lambda *a, **k: None)
    monkeypatch.setattr(email_send, "error_toast", lambda *a, **k: None)
    monkeypatch.setattr(email_send, "success_toast", lambda *a, **k: None)
    monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)

    async def hard_bounce(**kwargs):
        return False, "HARD_BOUNCE: 550 5.1.1 user unknown", None

    monkeypatch.setattr("emailer.sender.send_email", hard_bounce)
    monkeypatch.setattr(email_send, "get_conn", lambda: conn)
    monkeypatch.setattr(email_send, "_render_batch_send", lambda connection: None)
    monkeypatch.setattr(email_send, "_render_send_status", lambda: None)

    email_send.render_email_send()

    conn = sqlite3.connect(db_path)
    suppression = conn.execute(
        "SELECT reason, source FROM email_suppressions"
    ).fetchone()
    conn.close()
    assert suppression is None


class TestDomainClassification:
    """Tests for tiered domain classification."""

    def test_free_email_classification_gmail(self):
        from emailer.rotation import classify_domain, DomainTier
        tier, limit = classify_domain("fixture.sender09@gmail.com")
        assert tier == DomainTier.FREE_EMAIL
        assert limit == 25

    def test_free_email_classification_hotmail(self):
        from emailer.rotation import classify_domain, DomainTier
        tier, limit = classify_domain("someone@hotmail.com")
        assert tier == DomainTier.FREE_EMAIL

    def test_free_email_classification_yahoo(self):
        from emailer.rotation import classify_domain, DomainTier
        tier, limit = classify_domain("test@yahoo.com")
        assert tier == DomainTier.FREE_EMAIL

    def test_free_email_classification_outlook(self):
        from emailer.rotation import classify_domain, DomainTier
        tier, limit = classify_domain("user@outlook.com")
        assert tier == DomainTier.FREE_EMAIL

    def test_free_email_classification_qq(self):
        from emailer.rotation import classify_domain, DomainTier
        tier, limit = classify_domain("12345@qq.com")
        assert tier == DomainTier.FREE_EMAIL

    def test_free_email_classification_163(self):
        from emailer.rotation import classify_domain, DomainTier
        tier, limit = classify_domain("user@163.com")
        assert tier == DomainTier.FREE_EMAIL

    def test_business_domain_classification(self):
        from emailer.rotation import classify_domain, DomainTier
        tier, limit = classify_domain("john@acmecorp.com")
        assert tier == DomainTier.BUSINESS
        assert limit == 15

    def test_business_domain_regional_isp(self):
        """Legacy ISP domains (vnn.vn etc.) are now classified as BUSINESS,
        not silently dropped."""
        from emailer.rotation import classify_domain, DomainTier
        tier, limit = classify_domain("user@hcm.vnn.vn")
        assert tier == DomainTier.BUSINESS

    def test_case_insensitive_classification(self):
        from emailer.rotation import classify_domain, DomainTier
        tier, _ = classify_domain("User@Gmail.Com")
        assert tier == DomainTier.FREE_EMAIL

    def test_invalid_email_classification(self):
        from emailer.rotation import classify_domain, DomainTier
        tier, limit = classify_domain("not-an-email")
        assert tier == DomainTier.BUSINESS  # "unknown" → BUSINESS


class TestTieredDomainLimits:
    """Tests for tiered per-domain rate limiting."""

    def test_free_email_limit_enforced(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        # Send 25 emails to gmail.com — limit is 25
        for i in range(25):
            guard.record_send(f"user{i}@gmail.com")
        # 26th should be blocked
        ok, reason = guard.can_send("fixture.sender08@gmail.com")
        assert not ok
        assert "Domain limit reached" in reason
        assert "gmail.com" in reason

    def test_business_domain_limit_enforced(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        # Send 15 emails to acmecorp.com — limit is 15
        for i in range(15):
            guard.record_send(f"user{i}@acmecorp.com")
        # 16th should be blocked
        ok, reason = guard.can_send("user16@acmecorp.com")
        assert not ok
        assert "Domain limit reached" in reason
        assert "acmecorp.com" in reason

    def test_free_and_business_limits_independent(self, db_conn):
        """Free email and business domain limits are tracked separately."""
        guard = AntiSpamGuard(db_conn)
        # Max out gmail
        for i in range(25):
            guard.record_send(f"user{i}@gmail.com")
        # Max out a business domain
        for i in range(15):
            guard.record_send(f"user{i}@acmecorp.com")

        # Gmail should be blocked
        ok, _ = guard.can_send("fixture.extrfixture.sender01@gmail.com")
        assert not ok
        # Different business domain should still be allowed
        ok, _ = guard.can_send("new@othercorp.com")
        assert ok

    def test_status_includes_tier_info(self, db_conn):
        guard = AntiSpamGuard(db_conn)
        guard.record_send("fixture.sender01@gmail.com")
        guard.record_send("b@hotmail.com")
        guard.record_send("c@acmecorp.com")

        status = guard.get_status()
        assert "per_domain_limit" in status
        assert "free_email" in status["per_domain_limit"]
        assert "business" in status["per_domain_limit"]
        assert "tier_sent" in status
        assert "tier_limits" in status
        # FREE_EMAIL tier: 2 sends (gmail + hotmail)
        assert status["tier_sent"]["FREE_EMAIL"] == 2
        # BUSINESS tier: 1 send (acmecorp)
        assert status["tier_sent"]["BUSINESS"] == 1

    def test_mx_check_blocks_no_mx(self, db_conn, monkeypatch):
        """When MX check returns False, can_send should block with MX reason."""
        guard = AntiSpamGuard(db_conn)

        def mock_no_mx(domain):
            return False

        monkeypatch.setattr(
            "utils.email_verify.has_mx_record", mock_no_mx
        )
        # Need to re-import guard's _check_mx to pick up the mock
        ok, reason = guard.can_send("user@no-mx-domain.com")
        assert not ok
        assert "No MX record" in reason

    def test_mx_check_allows_with_mx(self, db_conn, monkeypatch):
        """When MX check returns True, can_send allows (assuming other checks pass)."""
        guard = AntiSpamGuard(db_conn)

        def mock_has_mx(domain):
            return True

        monkeypatch.setattr(
            "utils.email_verify.has_mx_record", mock_has_mx
        )
        ok, reason = guard.can_send("user@valid-domain.com")
        assert ok
        assert reason == ""


class TestCommunicationLogging:
    """Tests for _log_communication with Manager classes."""

    def test_log_communication_writes_record(self, db_conn):
        """After a successful send, communication record is written and
        lead is marked contacted."""
        from emailer.batch import _log_communication
        from crm.manager import LeadManager

        # Create a lead first
        lead_mgr = LeadManager(db_conn)
        from crm.models import Lead
        lead = lead_mgr.create(Lead(
            company_name="Test Corp",
            email="test@example.com",
            source="test",
        ))

        rendered = {
            "subject": "Test Subject",
            "body_html": "<p>Test Body</p>",
            "template_id": None,  # No FK reference needed for this test
        }

        _log_communication(db_conn, lead.id, rendered, "<msg-id-123@test>")

        # Verify communication was written
        row = db_conn.execute(
            "SELECT * FROM communications WHERE lead_id = ?", (lead.id,)
        ).fetchone()
        assert row is not None
        assert row["subject"] == "Test Subject"
        assert row["direction"] == "outbound"
        assert row["status"] == "sent"
        assert row["message_id"] == "<msg-id-123@test>"

        # Verify lead status was updated
        lead_after = lead_mgr.get_by_id(lead.id)
        assert lead_after.status == "contacted"
        assert lead_after.first_contacted_at is not None


class TestBatchLeadSelection:
    """Regression: `NOT IN (SELECT lead_id …)` returns 0 rows when the
    subquery contains NULL lead_ids (legacy 2026-05 sends). Selection must
    use a NULL-safe exclusion (NOT EXISTS)."""

    def test_eligible_leads_with_null_lead_id_comm(self, db_conn):
        from emailer.batch import _eligible_leads_query

        # 1) eligible lead
        db_conn.execute(
            "INSERT INTO leads (company_name, email, status, source, discovered_at) "
            "VALUES ('ACME', 'fixture.sender03@gmail.com', 'new', 'manual', '2026-08-01 00:00:00')"
        )
        # 2) already-contacted lead (must still be excluded)
        db_conn.execute(
            "INSERT INTO leads (company_name, email, status, source, discovered_at) "
            "VALUES ('Done Co', 'done@hotmail.com', 'new', 'manual', '2026-08-01 00:00:00')"
        )
        done_id = db_conn.execute(
            "SELECT id FROM leads WHERE company_name='Done Co'").fetchone()["id"]
        db_conn.execute(
            "INSERT INTO communications (lead_id, channel, direction, status, sent_at) "
            "VALUES (?, 'email', 'outbound', 'sent', '2026-08-01 00:00:00')", (done_id,)
        )
        # 3) poison row: outbound comm with NULL lead_id (legacy data) — the bug trigger
        db_conn.execute(
            "INSERT INTO communications (lead_id, channel, direction, status, sent_at) "
            "VALUES (NULL, 'email', 'outbound', 'sent', '2026-05-11 00:00:00')"
        )
        db_conn.commit()

        query, params = _eligible_leads_query(limit=10)
        rows = db_conn.execute(query, params).fetchall()
        assert [r["company_name"] for r in rows] == ["ACME"]

    def test_eligible_leads_excludes_contacted_only(self, db_conn):
        """Without any NULL lead_id rows, only genuinely-contacted leads are
        excluded — behavior unchanged from the original guard."""
        from emailer.batch import _eligible_leads_query

        for name, email in [("A", "fixture.sender01@gmail.com"), ("B", "fixture.sender02@gmail.com")]:
            db_conn.execute(
                "INSERT INTO leads (company_name, email, status, source, discovered_at) "
                "VALUES (?, ?, 'new', 'manual', '2026-08-01 00:00:00')", (name, email)
            )
        b_id = db_conn.execute(
            "SELECT id FROM leads WHERE company_name='B'").fetchone()["id"]
        db_conn.execute(
            "INSERT INTO communications (lead_id, channel, direction, status, sent_at) "
            "VALUES (?, 'email', 'outbound', 'sent', '2026-08-01 00:00:00')", (b_id,)
        )
        db_conn.commit()

        query, params = _eligible_leads_query(limit=10)
        rows = db_conn.execute(query, params).fetchall()
        assert [r["company_name"] for r in rows] == ["A"]

    def test_eligible_leads_source_filter(self, db_conn):
        """source= filters to one import source (e.g. 孚盟数据)."""
        from emailer.batch import _eligible_leads_query

        for name, email, source in [
            ("Fumu A", "a@fumu.com", "孚盟数据"),
            ("Fumu B", "b@fumu.com", "孚盟数据"),
            ("Manual C", "fixture.sender04@gmail.com", "manual"),
        ]:
            db_conn.execute(
                "INSERT INTO leads (company_name, email, status, source, discovered_at) "
                "VALUES (?, ?, 'new', ?, '2026-08-01 00:00:00')", (name, email, source)
            )
        db_conn.commit()

        query, params = _eligible_leads_query(limit=10, source="孚盟数据")
        rows = db_conn.execute(query, params).fetchall()
        assert sorted(r["company_name"] for r in rows) == ["Fumu A", "Fumu B"]

    def test_eligible_leads_source_empty_means_all(self, db_conn):
        """No source filter → all sources included (backward compatible)."""
        from emailer.batch import _eligible_leads_query

        for name, email, source in [
            ("Fumu A", "a@fumu.com", "孚盟数据"),
            ("Manual C", "fixture.sender04@gmail.com", "manual"),
        ]:
            db_conn.execute(
                "INSERT INTO leads (company_name, email, status, source, discovered_at) "
                "VALUES (?, ?, 'new', ?, '2026-08-01 00:00:00')", (name, email, source)
            )
        db_conn.commit()

        query, params = _eligible_leads_query(limit=10)
        rows = db_conn.execute(query, params).fetchall()
        assert sorted(r["company_name"] for r in rows) == ["Fumu A", "Manual C"]

    def test_eligible_leads_exclude_sources(self, db_conn):
        """exclude_sources keeps the 孚盟 pools out of the NetEase batch."""
        from emailer.batch import _eligible_leads_query

        for name, email, source in [
            ("Fumu Hi", "a@fumu.com", "孚盟数据"),
            ("Fumu Norm", "b@fumu.com", "孚盟数据普通"),
            ("Xunke X", "x@xk.com", "xunkexing"),
            ("Manual C", "fixture.sender04@gmail.com", "manual"),
        ]:
            db_conn.execute(
                "INSERT INTO leads (company_name, email, status, source, discovered_at) "
                "VALUES (?, ?, 'new', ?, '2026-08-01 00:00:00')", (name, email, source)
            )
        db_conn.commit()

        query, params = _eligible_leads_query(
            limit=10, exclude_sources=["孚盟数据", "孚盟数据普通"]
        )
        rows = db_conn.execute(query, params).fetchall()
        assert sorted(r["company_name"] for r in rows) == ["Manual C", "Xunke X"]

    def test_eligible_leads_source_wins_over_exclude(self, db_conn):
        """source= takes precedence — the Zoho 孚盟 driver relies on this."""
        from emailer.batch import _eligible_leads_query

        for name, email, source in [
            ("Fumu Hi", "a@fumu.com", "孚盟数据"),
            ("Xunke X", "x@xk.com", "xunkexing"),
        ]:
            db_conn.execute(
                "INSERT INTO leads (company_name, email, status, source, discovered_at) "
                "VALUES (?, ?, 'new', ?, '2026-08-01 00:00:00')", (name, email, source)
            )
        db_conn.commit()

        query, params = _eligible_leads_query(
            limit=10, source="孚盟数据", exclude_sources=["孚盟数据"]
        )
        rows = db_conn.execute(query, params).fetchall()
        assert [r["company_name"] for r in rows] == ["Fumu Hi"]


class TestBatchMxPrefilter:
    """MX pre-filter: send only the subset of the candidate pool that can
    actually receive mail. The query fetches a wider pool (limit × overscan)
    and `_mx_prefilter` keeps the first `limit` MX-valid leads in priority
    order — so a 200-batch isn't wasted on ~85% no-MX leads."""

    def test_eligible_leads_query_overscan_scales_limit(self, db_conn):
        """mx_overscan multiplies the SQL LIMIT — the wider pool is returned."""
        from emailer.batch import _eligible_leads_query

        for i in range(5):
            db_conn.execute(
                "INSERT INTO leads (company_name, email, status, source, discovered_at) "
                "VALUES (?, ?, 'new', 'manual', '2026-08-01 00:00:00')",
                (f"Co {i}", f"co{i}@mail.com"),
            )
        db_conn.commit()

        query, params = _eligible_leads_query(limit=2, mx_overscan=4)
        # LIMIT is 2×4=8, but only 5 leads exist → all 5 come back as candidates
        assert params[-1] == 8
        rows = db_conn.execute(query, params).fetchall()
        assert len(rows) == 5

    def test_eligible_leads_query_default_keeps_exact_limit(self, db_conn):
        """Without mx_overscan, behavior is unchanged (limit rows)."""
        from emailer.batch import _eligible_leads_query

        for i in range(5):
            db_conn.execute(
                "INSERT INTO leads (company_name, email, status, source, discovered_at) "
                "VALUES (?, ?, 'new', 'manual', '2026-08-01 00:00:00')",
                (f"Co {i}", f"co{i}@mail.com"),
            )
        db_conn.commit()

        query, params = _eligible_leads_query(limit=2)
        assert params[-1] == 2
        rows = db_conn.execute(query, params).fetchall()
        assert len(rows) == 2

    def test_mx_prefilter_keeps_only_valid_in_priority_order(self, db_conn, monkeypatch):
        """No-MX and empty emails are dropped; first `limit` valid kept in order.

        Pre-filter exits early once `limit` is reached (P5/P6 are never
        examined), so `dropped` counts only what was skipped to fill the batch.
        """
        from emailer.batch import _mx_prefilter
        from emailer.rotation import AntiSpamGuard

        def mock_mx(domain):
            return domain != "no-mx.com"

        monkeypatch.setattr("utils.email_verify.has_mx_record", mock_mx)

        guard = AntiSpamGuard(db_conn)
        candidates = [
            {"company_name": "P1", "email": "p1@no-mx.com"},
            {"company_name": "P2", "email": "p2@good.com"},
            {"company_name": "P3", "email": "p3@no-mx.com"},
            {"company_name": "P4", "email": "p4@good.com"},
            {"company_name": "P5", "email": ""},  # no email at all
            {"company_name": "P6", "email": "p6@good.com"},
        ]
        kept, dropped = _mx_prefilter(guard, candidates, limit=2)
        assert [c["company_name"] for c in kept] == ["P2", "P4"]
        assert dropped == 2  # P1 + P3 skipped; P5/P6 not reached (early exit)

    def test_mx_prefilter_short_pool_returns_what_it_has(self, db_conn, monkeypatch):
        """Fewer than `limit` valid leads → batch sends fewer, not zero."""
        from emailer.batch import _mx_prefilter
        from emailer.rotation import AntiSpamGuard

        def mock_mx(domain):
            return domain == "good.com"

        monkeypatch.setattr("utils.email_verify.has_mx_record", mock_mx)

        guard = AntiSpamGuard(db_conn)
        candidates = [
            {"company_name": "Only", "email": "only@good.com"},
            {"company_name": "Bad", "email": "bad@no-mx.com"},
        ]
        kept, dropped = _mx_prefilter(guard, candidates, limit=5)
        assert [c["company_name"] for c in kept] == ["Only"]
        assert dropped == 1


class TestSmtpProfile:
    """_smtp_settings picks NetEase (default) vs Zoho profile correctly."""

    def test_default_profile_uses_netease(self, monkeypatch):
        from emailer.sender import _smtp_settings
        import config as cfg

        monkeypatch.setattr(cfg, "SMTP_HOST", "smtp.qiye.163.com")
        monkeypatch.setattr(cfg, "SMTP_PORT", 465)
        monkeypatch.setattr(cfg, "SMTP_USE_TLS", False)
        monkeypatch.setattr(cfg, "SMTP_USERNAME", "sender@example.com")
        monkeypatch.setattr(cfg, "SMTP_FROM_NAME", "Foreign Trade Team")

        s = _smtp_settings("default")
        assert s["host"] == "smtp.qiye.163.com"
        assert s["port"] == 465
        assert s["username"] == "sender@example.com"
        assert s["from_name"] == "Foreign Trade Team"

    def test_zoho_profile_uses_zoho_settings(self, monkeypatch):
        from emailer.sender import _smtp_settings
        import config as cfg

        monkeypatch.setattr(cfg, "ZOHO_SMTP_HOST", "smtppro.zoho.com")
        monkeypatch.setattr(cfg, "ZOHO_SMTP_PORT", 465)
        monkeypatch.setattr(cfg, "ZOHO_SMTP_USE_TLS", False)
        monkeypatch.setattr(cfg, "ZOHO_SMTP_USERNAME", "operator@example.com")
        monkeypatch.setattr(cfg, "ZOHO_SMTP_FROM_EMAIL", "operator@example.com")
        monkeypatch.setattr(cfg, "ZOHO_SMTP_FROM_NAME", "Demo Sender")

        s = _smtp_settings("zoho")
        assert s["host"] == "smtppro.zoho.com"
        assert s["port"] == 465
        assert s["use_tls"] is False  # direct SSL on 465
        assert s["username"] == "operator@example.com"
        assert s["from_email"] == "operator@example.com"
        assert s["from_name"] == "Demo Sender"


class TestMailboxQuotaSeparation:
    """Zoho's 20/day quota must be independent of the NetEase batch quota."""

    def test_zoho_sends_do_not_touch_netease_quota(self, db_conn):
        netease = AntiSpamGuard(db_conn, mailbox="default")
        zoho = AntiSpamGuard(db_conn, mailbox="zoho")

        zoho.record_send("a@company.com")
        zoho.record_send("fixture.sender02@gmail.com")

        # Zoho row: 2 sends recorded
        assert zoho.get_status()["emails_sent_today"] == 2
        # NetEase row: untouched
        assert netease.get_status()["emails_sent_today"] == 0

    def test_netease_sends_do_not_touch_zoho_quota(self, db_conn):
        netease = AntiSpamGuard(db_conn, mailbox="default")
        zoho = AntiSpamGuard(db_conn, mailbox="zoho")

        netease.record_send("x@corp.com")

        assert netease.get_status()["emails_sent_today"] == 1
        assert zoho.get_status()["emails_sent_today"] == 0

    def test_zoho_domain_counts_are_per_mailbox(self, db_conn):
        netease = AntiSpamGuard(db_conn, mailbox="default")
        zoho = AntiSpamGuard(db_conn, mailbox="zoho")

        netease.record_send("a@corp.com")
        netease.record_send("b@corp.com")  # same domain via NetEase
        zoho.record_send("c@corp.com")     # same domain via Zoho

        assert netease.get_status()["domain_counts"].get("corp.com") == 2
        assert zoho.get_status()["domain_counts"].get("corp.com") == 1

    def test_zoho_daily_cap_enforced_independently(self, db_conn, monkeypatch):
        import config as cfg
        monkeypatch.setattr(cfg, "ZOHO_DAILY_LIMIT", 2)

        zoho = AntiSpamGuard(db_conn, mailbox="zoho")
        netease = AntiSpamGuard(db_conn, mailbox="default")

        zoho.record_send("a@x.com")
        zoho.record_send("b@y.com")
        ok, reason = zoho.can_send("c@z.com")
        assert not ok
        assert "Daily limit reached (2)" in reason

        # NetEase is unaffected by Zoho's cap (example.com passes MX)
        ok, _ = netease.can_send("d@example.com")
        assert ok
