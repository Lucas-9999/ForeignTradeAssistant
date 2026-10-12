"""
Tests for IMAP inbox monitor — focus on bounce/auto-reply classification.

The 7-layer classification filter is the most critical component:
false positives (bounce classified as reply) would cause the system
to treat delivery failures as customer responses.
"""

import email
import sqlite3
import tempfile
from pathlib import Path

import pytest

from emailer.inbox import (
    InboxMonitor,
    MessageClass,
    _decode_header_value,
    _extract_email_address,
)

# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_msg(from_header="user@example.com", subject="Hello", body="Thanks",
              extra_headers=None, to_addr=None):
    """Build a minimal email.message.Message for testing.

    By default, sets To to the configured SMTP username so the
    'is this addressed to us?' check passes in test environments.
    Does NOT set Return-Path (tests that need it should add it explicitly).
    """
    import config as _config
    msg = email.message.EmailMessage()
    msg["From"] = from_header
    msg["Subject"] = subject
    msg["To"] = to_addr or _config.SMTP_USERNAME or "test@momafastener.com"
    msg["Date"] = "Mon, 20 Jul 2026 14:30:00 +0800"
    msg.set_content(body)
    if extra_headers:
        for key, value in extra_headers.items():
            # Return-Path / Date already exist on the message (Date is set
            # above) — replace rather than add a duplicate header.
            if key in ("Return-Path", "Date"):
                try:
                    msg.replace_header(key, value)
                except KeyError:
                    msg[key] = value
            else:
                msg[key] = value
    return msg


@pytest.fixture
def db_conn():
    """Create an in-memory SQLite database with communications schema."""
    from crm.db import DB_SCHEMA

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def monitor(db_conn):
    """Create an InboxMonitor with test DB connection."""
    return InboxMonitor(db_conn)


# ═══════════════════════════════════════════════════════════════════════════
# Header decoding
# ═══════════════════════════════════════════════════════════════════════════


class TestHeaderDecoding:
    def test_plain_text(self):
        assert _decode_header_value("Hello World") == "Hello World"

    def test_none(self):
        assert _decode_header_value(None) == ""

    def test_utf8_base64(self):
        result = _decode_header_value("=?UTF-8?B?5rWL6K+V?=")
        assert len(result) > 0  # Should decode to something

    def test_extract_email_with_brackets(self):
        assert _extract_email_address("John <john@test.com>") == "john@test.com"

    def test_extract_email_bare(self):
        assert _extract_email_address("john@test.com") == "john@test.com"

    def test_extract_email_none(self):
        assert _extract_email_address(None) == ""


# ═══════════════════════════════════════════════════════════════════════════
# Classification — BOUNCE detection (user-critical: 163.com filter)
# ═══════════════════════════════════════════════════════════════════════════


class TestBounceClassification:
    """Verify ALL 7 layers of bounce detection work correctly."""

    def test_bounce_mailer_daemon_from(self, monitor):
        """Layer 1: mailer-daemon@* sender → BOUNCE"""
        msg = _make_msg(from_header="mailer-daemon@googlemail.com",
                        subject="Mail Delivery Failed")
        assert monitor._classify_message(msg) == MessageClass.BOUNCE

    def test_bounce_postmaster_from(self, monitor):
        """Layer 1: postmaster@* sender → BOUNCE"""
        msg = _make_msg(from_header="postmaster@163.com",
                        subject="Undelivered Mail")
        assert monitor._classify_message(msg) == MessageClass.BOUNCE

    def test_bounce_domain_from(self, monitor):
        """Layer 1: @bounce.* domain → BOUNCE"""
        msg = _make_msg(from_header="system@bounce.163.com",
                        subject="Some notification")
        assert monitor._classify_message(msg) == MessageClass.BOUNCE

    def test_auto_submitted_header(self, monitor):
        """Layer 2: Auto-Submitted: auto-replied → AUTO_REPLY"""
        msg = _make_msg(from_header="someone@customer.com",
                        subject="Re: Your email",
                        extra_headers={"Auto-Submitted": "auto-replied"})
        assert monitor._classify_message(msg) == MessageClass.AUTO_REPLY

    def test_dsn_with_auto_submitted_still_bounce(self, monitor):
        """A DSN carrying Auto-Submitted (Trend Micro etc.) stays a BOUNCE.

        Real delivery-failure notices observed in production set
        Auto-Submitted: auto-replied. The bounce subject keyword must beat
        the Auto-Submitted signal, or a hard bounce is miscounted as an
        auto-reply and never reaches the CRM 今日退信 count.
        """
        msg = _make_msg(
            from_header="no-reply@tmes.trendmicro.eu (Mail Delivery System)",
            subject="Undelivered Mail Returned to Sender",
            extra_headers={"Auto-Submitted": "auto-replied"},
        )
        assert monitor._classify_message(msg) == MessageClass.BOUNCE
        # Genuine out-of-office with a non-bounce subject still → AUTO_REPLY.
        msg2 = _make_msg(
            from_header="Info <info@oslvitalia.it>",
            subject="Risposta automatica: Fastener quotes within 48 hours",
            extra_headers={"Auto-Submitted": "auto-generated"},
        )
        assert monitor._classify_message(msg2) == MessageClass.AUTO_REPLY

    def test_empty_return_path(self, monitor):
        """Layer 5: empty Return-Path → BOUNCE"""
        msg = _make_msg(from_header="Mail System <system@mx.google.com>",
                        subject="Delivery Status",
                        extra_headers={"Return-Path": "<>"})
        assert monitor._classify_message(msg) == MessageClass.BOUNCE

    def test_bounce_subject_english(self, monitor):
        """Layer 4a: English bounce subjects → BOUNCE"""
        subjects = [
            "Undelivered Mail Returned to Sender",
            "Returned mail: see transcript for details",
            "Delivery Status Notification (Failure)",
            "failure notice",
        ]
        for subj in subjects:
            msg = _make_msg(subject=subj)
            assert monitor._classify_message(msg) == MessageClass.BOUNCE, \
                f"Failed for subject: {subj}"

    def test_multilingual_provider_bounce_subjects(self, monitor):
        """Provider-localized DSNs seen in the 163 inbox are bounces."""
        subjects = [
            "Nedostavljivo: Fastener supply",
            "Non remis : Quick follow-up",
            "Unzustellbar: Fastener Supply Cooperation",
            "Unknown address / nieznany adres",
            "Problema ao entregar o e-mail - retorno ao remetente",
            "Rejected: Looking for a reliable fastener partner in China?",
        ]
        for subject in subjects:
            msg = _make_msg(
                subject=subject,
                extra_headers={"Return-Path": "<system@example.com>"},
            )
            assert monitor._classify_message(msg) == MessageClass.BOUNCE

    def test_bounce_subject_chinese_163(self, monitor):
        """Layer 4b: Chinese bounce subjects (163.com style) → BOUNCE"""
        subjects = [
            "系统退信",
            "退信通知",
            "邮件投递失败通知",
            "邮件被退回",
            "发送失败：邮件无法送达",
            "邮件未送达",
            "邮箱不存在退信",
        ]
        for subj in subjects:
            msg = _make_msg(subject=subj)
            assert monitor._classify_message(msg) == MessageClass.BOUNCE, \
                f"Failed for subject: {subj}"

    def test_bounce_body_keywords(self, monitor):
        """Layer 7: Bounce diagnostics in body → BOUNCE"""
        bodies = [
            "The following address failed: test@example.com",
            "550 Mailbox not found",
            "User unknown",
            "收件人邮件地址不存在",
            "邮箱不存在，邮件无法送达",
        ]
        for body in bodies:
            msg = _make_msg(
                from_header="mail-system@someisp.com",
                subject="Notification",
                body=body,
            )
            assert monitor._classify_message(msg) == MessageClass.BOUNCE, \
                f"Failed for body: {body}"

    def test_precedence_bulk(self, monitor):
        """Layer 4: Precedence: bulk → UNKNOWN"""
        msg = _make_msg(from_header="newsletter@company.com",
                        subject="Newsletter",
                        extra_headers={"Precedence": "bulk",
                                       "Return-Path": "<newsletter@company.com>"})
        assert monitor._classify_message(msg) == MessageClass.UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════
# Classification — AUTO_REPLY detection (out-of-office etc.)
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoReplyClassification:
    def test_out_of_office_subject(self, monitor):
        """Auto-reply subject → AUTO_REPLY"""
        subjects = [
            "Out of Office: Your email",
            "Auto reply: Away from desk",
            "Automatic reply: On vacation",
        ]
        for subj in subjects:
            msg = _make_msg(from_header="someone@customer.com",
                            subject=subj)
            assert monitor._classify_message(msg) == MessageClass.AUTO_REPLY, \
                f"Failed for subject: {subj}"

    def test_auto_submitted_generated(self, monitor):
        """Auto-Submitted: auto-generated → AUTO_REPLY"""
        msg = _make_msg(from_header="noreply@company.com",
                        subject="Thank you for your email",
                        extra_headers={"Auto-Submitted": "auto-generated"})
        assert monitor._classify_message(msg) == MessageClass.AUTO_REPLY


# ═══════════════════════════════════════════════════════════════════════════
# Classification — genuine REPLY (should pass all filters)
# ═══════════════════════════════════════════════════════════════════════════


class TestReplyClassification:
    def test_genuine_reply(self, monitor):
        """A real customer reply should pass all filters → REPLY"""
        msg = _make_msg(
            from_header="James Buyer <buyer@fastener-co.com>",
            subject="Re: Quick question about Fastener Co's supply",
            body="Hi Lucas, yes I'm interested. Please send pricing.",
            extra_headers={
                "In-Reply-To": "<abc123@momafastener.com>",
                "Return-Path": "<buyer@fastener-co.com>",
            },
        )
        assert monitor._classify_message(msg) == MessageClass.REPLY

    def test_reply_with_chinese_prefix(self, monitor):
        """Reply with 回复: prefix → REPLY"""
        msg = _make_msg(
            from_header="buyer@china-co.cn",
            subject="回复: Quick question about supply",
            body="请发报价单",
            extra_headers={"Return-Path": "<buyer@china-co.cn>"},
        )
        assert monitor._classify_message(msg) == MessageClass.REPLY

    def test_reply_no_in_reply_to(self, monitor):
        """Reply without In-Reply-To header → still REPLY (match falls to lower priority)"""
        msg = _make_msg(
            from_header="buyer@example.de",
            subject="Re: Fastener supplier inquiry",
            body="Yes, please send more info.",
            extra_headers={"Return-Path": "<buyer@example.de>"},
        )
        assert monitor._classify_message(msg) == MessageClass.REPLY

    def test_unthreaded_non_reply_is_unknown(self, monitor):
        msg = _make_msg(
            subject="Your email requires verification",
            body="Please click this verification link.",
            extra_headers={"Return-Path": "<system@example.com>"},
        )
        assert monitor._classify_message(msg) == MessageClass.UNKNOWN

    def test_persisted_inbound_classifier_separates_reply_and_bounce(self):
        from emailer.inbox import classify_persisted_inbound

        assert classify_persisted_inbound(
            "Re: Information Completion Request",
            "Please quote me standard parts.",
        ) == MessageClass.REPLY
        assert classify_persisted_inbound(
            "Undelivered Mail Returned to Sender",
            "Delivery failed.",
        ) == MessageClass.BOUNCE
        assert classify_persisted_inbound(
            "Auto: Fastener quotation",
            "Automated response",
        ) == MessageClass.AUTO_REPLY

    def test_reply_subject_normalizes_gateway_markers(self):
        from emailer.inbox import _strip_reply_prefixes

        assert _strip_reply_prefixes(
            "Re: [DCE] Re: Fwd: Purchase of Fasteners"
        ) == "Purchase of Fasteners"

    def test_unknown_when_not_to_us(self, monitor):
        """Email not addressed to us → UNKNOWN"""
        msg = _make_msg(
            from_header="fixture.sender07@gmail.com",
            subject="Some random thread",
            to_addr="other@company.com",  # Explicitly NOT our address
            extra_headers={"Return-Path": "<fixture.sender07@gmail.com>"},
        )
        assert monitor._classify_message(msg) == MessageClass.UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════
# Subject prefix stripping
# ═══════════════════════════════════════════════════════════════════════════


class TestSubjectPrefixStripping:
    def test_re_prefix(self):
        from emailer.inbox import _strip_reply_prefixes
        assert _strip_reply_prefixes("Re: Hello") == "Hello"

    def test_chinese_prefix(self):
        from emailer.inbox import _strip_reply_prefixes
        assert _strip_reply_prefixes("回复: Hello") == "Hello"

    def test_multiple_prefixes(self):
        from emailer.inbox import _strip_reply_prefixes
        assert _strip_reply_prefixes("Re: AW: Hello") == "Hello"

    def test_triple_prefixes(self):
        from emailer.inbox import _strip_reply_prefixes
        assert _strip_reply_prefixes("Fwd: Re: 回复: Hello") == "Hello"

    def test_no_prefix(self):
        from emailer.inbox import _strip_reply_prefixes
        assert _strip_reply_prefixes("Plain subject") == "Plain subject"


# ═══════════════════════════════════════════════════════════════════════════
# Integration: InboxMonitor with DB
# ═══════════════════════════════════════════════════════════════════════════


class TestInboxMonitorDB:
    def test_monitor_init(self, db_conn):
        """InboxMonitor can be created with a DB connection."""
        monitor = InboxMonitor(db_conn)
        assert monitor.host is not None
        assert monitor.port is not None

    def test_imap_uid_dedup(self, db_conn, monitor):
        """imap_uid_exists correctly detects already-processed messages."""
        # Should not exist initially
        assert not monitor.comm_mgr.imap_uid_exists("test-uid-99999")

    def test_check_inbox_no_credentials(self, db_conn, monitor):
        """check_inbox returns error when IMAP is not configured.

        Force an empty-credentials state on the monitor regardless of what
        .env contains, so the test never touches a real IMAP mailbox.
        """
        monitor.username = ""
        monitor.password = ""
        result = monitor.check_inbox(hours_back=1)
        assert "IMAP credentials not configured" in result.errors


# ═══════════════════════════════════════════════════════════════════════════
# Failed-recipient extraction from DSN messages
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractFailedRecipient:
    def test_final_recipient_header(self):
        from emailer.inbox import extract_failed_recipient
        msg = _make_msg(subject="Undelivered Mail")
        msg["Final-Recipient"] = "rfc822;victim@target.com"
        assert extract_failed_recipient(msg) == ("victim@target.com", True)

    def test_original_recipient_header_with_display(self):
        from emailer.inbox import extract_failed_recipient
        msg = _make_msg(subject="Delivery Status Notification (Failure)")
        msg["Original-Recipient"] = "rfc822; buyer@example.de"
        assert extract_failed_recipient(msg) == ("buyer@example.de", True)

    def test_x_failed_recipients_header(self):
        from emailer.inbox import extract_failed_recipient
        msg = _make_msg(subject="Returned mail")
        msg["X-Failed-Recipients"] = "<user@example.fr>"
        assert extract_failed_recipient(msg) == ("user@example.fr", True)

    def test_body_fallback_is_not_reliable(self):
        from emailer.inbox import extract_failed_recipient
        msg = _make_msg(
            subject="系统退信",
            body="您的邮件无法送达。收件人地址：john@smith.de",
        )
        assert extract_failed_recipient(msg) == ("john@smith.de", False)

    def test_body_excludes_own_address(self, monkeypatch):
        import config
        from emailer.inbox import extract_failed_recipient
        # The showcase has no inherited mailbox credentials. Declare the
        # synthetic sender explicitly instead of relying on local .env state.
        own = "sender@portfolio.invalid"
        monkeypatch.setattr(config, "SMTP_USERNAME", own)
        msg = _make_msg(
            subject="退信通知",
            body=f"发件人 {own}\n您的邮件未能发送到 buyer@example.net",
        )
        assert extract_failed_recipient(msg) == ("buyer@example.net", False)

    def test_no_recipient(self):
        from emailer.inbox import extract_failed_recipient
        msg = _make_msg(subject="Delivery", body="No mailbox to identify")
        assert extract_failed_recipient(msg) == (None, False)


# ═══════════════════════════════════════════════════════════════════════════
# Bounce persistence (08-20: async DSNs were counted but never recorded)
# ═══════════════════════════════════════════════════════════════════════════


class TestBouncePersistence:
    def test_record_bounce_persists_and_dedups(self, db_conn, monitor):
        msg = _make_msg(
            from_header="postmaster@163.com",
            subject="系统退信",
            body="550 5.1.1 user unknown。收件人：victim@nowhere.com",
            extra_headers={"Final-Recipient": "rfc822;victim@nowhere.com"},
        )
        monitor._record_bounce(msg, "uid-b1")
        monitor._record_bounce(msg, "uid-b1")  # same UID → dedup

        rows = db_conn.execute(
            "SELECT * FROM communications WHERE direction='inbound' AND status='bounced'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["imap_uid"] == "uid-b1"
        assert rows[0]["subject"] == "系统退信"
        # Only a confirmed recipient hard bounce enters canonical suppression;
        # IMAP must not bypass P2/P3 through the legacy blacklist.
        suppression = db_conn.execute(
            "SELECT reason, source FROM email_suppressions "
            "WHERE email='victim@nowhere.com'"
        ).fetchone()
        assert suppression is not None
        assert tuple(suppression) == ("hard_bounce", "smtp:default")
        assert db_conn.execute(
            "SELECT 1 FROM blacklist WHERE email='victim@nowhere.com'"
        ).fetchone() is None

    def test_record_bounce_policy_rejection_does_not_suppress_recipient(
        self, db_conn, monitor
    ):
        """A DSN header proves recipient identity, not that a policy failure
        is a permanent recipient hard bounce."""
        msg = _make_msg(
            from_header="postmaster@163.com",
            subject="Delivery Status Notification (Failure)",
            body="550 5.7.1 policy rejection: message content blocked",
            extra_headers={"Final-Recipient": "rfc822;buyer@example.com"},
        )

        monitor._record_bounce(msg, "uid-policy")

        assert db_conn.execute(
            "SELECT 1 FROM communications WHERE imap_uid='uid-policy'"
        ).fetchone() is not None
        assert db_conn.execute(
            "SELECT 1 FROM email_suppressions WHERE email='buyer@example.com'"
        ).fetchone() is None
        assert db_conn.execute(
            "SELECT 1 FROM blacklist WHERE email='buyer@example.com'"
        ).fetchone() is None

    def test_invalid_dsn_recipient_is_recorded_without_suppression(
        self, db_conn, monitor
    ):
        """A permissive DSN header match can still fail strict address
        validation. It must not abort bounce accounting or trigger fallback
        suppression."""
        msg = _make_msg(
            from_header="postmaster@163.com",
            subject="Delivery Status Notification (Failure)",
            body="550 5.1.1 user unknown",
            extra_headers={"Final-Recipient": "rfc822;a@-b.com"},
        )

        monitor._record_bounce(msg, "uid-invalid-recipient")

        assert db_conn.execute(
            "SELECT 1 FROM communications WHERE imap_uid='uid-invalid-recipient'"
        ).fetchone() is not None
        assert db_conn.execute(
            "SELECT 1 FROM email_suppressions WHERE email='a@-b.com'"
        ).fetchone() is None
        assert db_conn.execute(
            "SELECT 1 FROM blacklist WHERE email='a@-b.com'"
        ).fetchone() is None

    def test_record_bounce_links_lead_by_recipient(self, db_conn, monitor):
        db_conn.execute(
            "INSERT INTO leads (company_name, email) VALUES ('Victim Co', 'victim@nowhere.com')"
        )
        db_conn.commit()
        msg = _make_msg(
            from_header="postmaster@163.com",
            subject="退信通知",
            extra_headers={"Final-Recipient": "rfc822;victim@nowhere.com"},
        )
        monitor._record_bounce(msg, "uid-b2")

        row = db_conn.execute(
            "SELECT lead_id FROM communications WHERE imap_uid='uid-b2'"
        ).fetchone()
        assert row is not None and row["lead_id"] is not None

    def test_record_bounce_without_recipient_still_persists(self, db_conn, monitor):
        msg = _make_msg(
            from_header="postmaster@163.com",
            subject="Delivery Status Notification (Failure)",
            body="The original message was rejected.",
        )
        monitor._record_bounce(msg, "uid-b3")
        rows = db_conn.execute(
            "SELECT * FROM communications WHERE imap_uid='uid-b3'"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "bounced"

    def test_record_bounce_body_only_recipient_not_blacklisted(self, db_conn, monitor):
        """Body-only recipient extraction links the lead but must NOT auto
        suppress — a misclassified message's body can contain unrelated
        addresses, and blacklisting it would wrongly block a real contact."""
        db_conn.execute(
            "INSERT INTO leads (company_name, email) VALUES ('Victim Co', 'victim@nowhere.com')"
        )
        db_conn.commit()
        msg = _make_msg(
            from_header="postmaster@163.com",
            subject="系统退信",
            body="您的邮件无法送达。收件人：victim@nowhere.com",  # no DSN header
        )
        monitor._record_bounce(msg, "uid-b4")
        row = db_conn.execute(
            "SELECT lead_id FROM communications WHERE imap_uid='uid-b4'"
        ).fetchone()
        assert row is not None and row["lead_id"] is not None
        bl = db_conn.execute(
            "SELECT * FROM blacklist WHERE email='victim@nowhere.com'"
        ).fetchone()
        assert bl is None

    def test_record_bounce_uses_dsn_date_not_scan_time(self, db_conn, monitor):
        """Backfilled DSNs keep their real Date so 今日退信 attributes each
        bounce to the day it actually happened (backfill from 08-01)."""
        msg = _make_msg(
            from_header="postmaster@163.com",
            subject="Delivery Status Notification (Failure)",
            extra_headers={
                "Final-Recipient": "rfc822;victim@nowhere.com",
                # DSN generated on 08-05 in a +0530 zone → 2026-08-05 09:12 UTC
                "Date": "Wed, 05 Aug 2026 14:42:00 +0530",
            },
        )
        monitor._record_bounce(msg, "uid-b5")
        row = db_conn.execute(
            "SELECT sent_at FROM communications WHERE imap_uid='uid-b5'"
        ).fetchone()
        assert row is not None
        assert row["sent_at"].startswith("2026-08-05 "), row["sent_at"]
        # unparseable Date → fallback to now (non-null)
        msg2 = _make_msg(
            from_header="postmaster@163.com",
            subject="Delivery Status Notification (Failure)",
        )
        del msg2["Date"]
        monitor._record_bounce(msg2, "uid-b6")
        row2 = db_conn.execute(
            "SELECT sent_at FROM communications WHERE imap_uid='uid-b6'"
        ).fetchone()
        assert row2 is not None and row2["sent_at"] is not None
