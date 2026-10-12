"""
Tests for the Zoho sent-follow-up tracker (engine/sent_tracker.py + emailer/zoho.py).

Mirrors tests/test_inbox.py style: in-memory DB_SCHEMA conn, synthetic messages,
and a FakeZohoClient so no real mailbox is ever touched. The autouse
_isolate_production_db fixture in conftest.py protects data/fta.db.
"""

import email
import sqlite3
from pathlib import Path

import pytest

import config
from crm.db import DB_SCHEMA
from crm.manager import CommunicationManager, LeadManager
from crm.models import Communication, Lead
from emailer.zoho import ZohoMailClient, ZohoSentMessage
from engine.sent_tracker import (
    NEW_CONTACT_NOTE,
    VETERAN_FOLLOWUP_NOTE,
    ClassifyBasis,
    RecipientClass,
    ZohoSentTracker,
)

NEW_MARKER = NEW_CONTACT_NOTE

SELF = "operator@example.com"


# ═══════════════════════════════════════════════════════════════════════════
# Fakes + helpers
# ═══════════════════════════════════════════════════════════════════════════


class FakeZohoClient:
    """In-memory stand-in for ZohoMailClient — no network."""

    def __init__(self, sent_messages=None, inbox_history=None, has_credentials=True):
        self.sent_messages = sent_messages or []
        self.inbox_history = {e.lower() for e in (inbox_history or [])}
        self._has_credentials = has_credentials
        self.closed = False

    @property
    def has_credentials(self):
        return self._has_credentials

    def connect(self):
        return object()

    def close(self):
        self.closed = True

    def scan_sent(self, days=1, max_messages=0):
        return self.sent_messages

    def inbox_has_from(self, email_addr):
        return email_addr.lower() in self.inbox_history


def _make_sent_msg(
    to=None,
    cc=None,
    subject="Follow up",
    message_id="real-msg@zoho.com",
    date_utc="2026-08-04 10:00:00",
    uid="100",
    preview="Hi there",
):
    """Build a ZohoSentMessage as returned by scan_sent."""
    to = to or ["buyer@acme.com"]
    return ZohoSentMessage(
        message_id=message_id,
        subject=subject,
        date_utc=date_utc,
        recipients=list(to) + list(cc or []),
        to_emails=list(to),
        cc_emails=list(cc or []),
        imap_uid=uid,
        body_preview=preview,
    )


@pytest.fixture
def db_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def comm_mgr(db_conn):
    return CommunicationManager(db_conn)


@pytest.fixture
def lead_mgr(db_conn):
    return LeadManager(db_conn)


def _tracker(db_conn, client, log_dir):
    return ZohoSentTracker(db_conn, client=client, log_dir=log_dir)


# ═══════════════════════════════════════════════════════════════════════════
# ZohoMailClient._parse_sent (pure parsing — no network)
# ═══════════════════════════════════════════════════════════════════════════


class TestParseSent:
    def _raw_msg(self, **overrides):
        msg = email.message.EmailMessage()
        msg["From"] = SELF
        msg["To"] = overrides.get("to", "Buyer <buyer@acme.com>, seller@vendor.com")
        msg["Cc"] = overrides.get("cc", "Cc <cc@example.com>")
        msg["Subject"] = overrides.get("subject", "Re: Quote")
        msg["Message-ID"] = overrides.get("message_id", "<real-msg@zoho.com>")
        msg["Date"] = overrides.get("date", "Mon, 04 Aug 2026 10:00:00 +0800")
        msg.set_content(overrides.get("body", "Hi"))
        return msg

    def _client(self):
        return ZohoMailClient(
            host="imap.zoho.com", port=993, username=SELF, password="x"
        )

    def test_parses_recipients_and_headers(self):
        sent, _ = self._client()._parse_sent(self._raw_msg(), uid="42")
        assert sent.message_id == "real-msg@zoho.com"
        assert sent.recipients == ["buyer@acme.com", "seller@vendor.com", "cc@example.com"]
        assert sent.subject == "Re: Quote"
        assert sent.imap_uid == "42"
        assert sent.body_preview == "Hi"

    def test_date_converted_to_utc(self):
        sent, _ = self._client()._parse_sent(self._raw_msg(), uid="42")
        assert sent.date_utc == "2026-08-04 02:00:00"

    def test_self_address_dropped(self):
        to = f"Me <{SELF}>, buyer@acme.com"
        sent, _ = self._client()._parse_sent(self._raw_msg(to=to, cc=""), uid="42")
        assert SELF not in sent.recipients
        assert sent.recipients == ["buyer@acme.com"]

    def test_message_id_falls_back_to_uid(self):
        msg = self._raw_msg()
        msg.replace_header("Message-ID", "")
        sent, _ = self._client()._parse_sent(msg, uid="99")
        assert sent.message_id == "99"


# ═══════════════════════════════════════════════════════════════════════════
# ZohoSentTracker
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def tmp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", tmp_path)
    return tmp_path


class TestCredentialsGuard:
    def test_no_credentials_skips(self, db_conn, tmp_log):
        client = FakeZohoClient(has_credentials=False)
        result = _tracker(db_conn, client, tmp_log).run()
        assert result.errors
        assert "credentials" in result.errors[0].lower()
        assert result.scanned == 0
        assert db_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
        assert db_conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 0


class TestClassification:
    def test_veteran_with_inbox_history(self, db_conn, tmp_log):
        client = FakeZohoClient(
            sent_messages=[_make_sent_msg(to=["buyer@acme.com"])],
            inbox_history=["buyer@acme.com"],
        )
        result = _tracker(db_conn, client, tmp_log).run(report_only=True)
        assert result.veterans and result.veterans[0].classification == RecipientClass.VETERAN
        assert result.veterans[0].basis == ClassifyBasis.INBOX_HISTORY
        assert result.new_contacts == []

    def test_new_without_history(self, db_conn, tmp_log):
        client = FakeZohoClient(
            sent_messages=[_make_sent_msg(to=["purchaser@vendor.com"])],
            inbox_history=[],
        )
        result = _tracker(db_conn, client, tmp_log).run(report_only=True)
        assert result.new_contacts and result.new_contacts[0].classification == RecipientClass.NEW
        assert result.new_contacts[0].basis == ClassifyBasis.DEFAULT
        assert result.veterans == []


class TestSubjectClassification:
    """Subject-line markers are the user's authoritative bucket signal."""

    def test_veteran_subject_marker_wins_without_inbox_history(self, db_conn, tmp_log):
        client = FakeZohoClient(
            sent_messages=[
                _make_sent_msg(to=["old@acme.com"], subject="Follow-up Email From JinSixi — 报价")
            ],
            inbox_history=[],  # subject marker decides even with no history
        )
        result = _tracker(db_conn, client, tmp_log).run(report_only=True)
        assert result.veterans and result.veterans[0].classification == RecipientClass.VETERAN
        assert result.veterans[0].basis == ClassifyBasis.SUBJECT
        assert result.new_contacts == []

    def test_company_information_veteran_marker(self, db_conn, tmp_log):
        """163 企业邮箱往来客户 — 主管转发，Zoho 收件箱无历史，但主题标记判定老客户."""
        client = FakeZohoClient(
            sent_messages=[
                _make_sent_msg(
                    to=["fixture.sender05@gmail.com"],
                    subject="Follow-up on Our Company Information",
                )
            ],
            inbox_history=[],  # no Zoho history — 163 企业邮箱里有往来
        )
        result = _tracker(db_conn, client, tmp_log).run(report_only=True)
        assert result.veterans and result.veterans[0].classification == RecipientClass.VETERAN
        assert result.veterans[0].basis == ClassifyBasis.SUBJECT
        assert result.new_contacts == []

    def test_new_subject_marker_overrides_inbox_history(self, db_conn, tmp_log):
        client = FakeZohoClient(
            sent_messages=[
                _make_sent_msg(to=["prospect@vendor.com"], subject="Fastener supplier introduction")
            ],
            inbox_history=["prospect@vendor.com"],  # history exists, but subject says NEW
        )
        result = _tracker(db_conn, client, tmp_log).run(report_only=True)
        assert result.new_contacts and result.new_contacts[0].classification == RecipientClass.NEW
        assert result.new_contacts[0].basis == ClassifyBasis.SUBJECT
        assert result.veterans == []

    def test_marker_match_is_case_insensitive(self, db_conn, tmp_log):
        client = FakeZohoClient(
            sent_messages=[
                _make_sent_msg(to=["a@one.com"], subject="fastener SUPPLIER Introduction — ABC"),
            ]
        )
        result = _tracker(db_conn, client, tmp_log).run(report_only=True)
        assert result.new_contacts[0].classification == RecipientClass.NEW

    def test_new_subject_marker_records_new_lead(self, db_conn, tmp_log, lead_mgr):
        client = FakeZohoClient(
            sent_messages=[
                _make_sent_msg(to=["purchaser@factory.com"], subject="Fastener supplier introduction")
            ]
        )
        result = _tracker(db_conn, client, tmp_log).run()
        assert result.leads_created == 1
        lead = lead_mgr.find_by_email("purchaser@factory.com")
        assert lead.status == "contacted"
        assert lead.source == "zoho"
        assert NEW_MARKER in (lead.notes or "")

    def test_veteran_auto_created_lead_gets_veteran_note(self, db_conn, tmp_log, lead_mgr):
        client = FakeZohoClient(
            sent_messages=[
                _make_sent_msg(to=["old@acme.com"], subject="Follow-up Email From JinSixi — 报价")
            ]
        )
        result = _tracker(db_conn, client, tmp_log).run()
        assert result.leads_created == 1
        lead = lead_mgr.find_by_email("old@acme.com")
        assert VETERAN_FOLLOWUP_NOTE in (lead.notes or "")
        assert NEW_MARKER not in (lead.notes or "")

    def test_new_lead_contact_timestamps_from_send_date(self, db_conn, tmp_log, lead_mgr):
        client = FakeZohoClient(
            sent_messages=[
                _make_sent_msg(
                    to=["purchaser@factory.com"],
                    subject="Fastener supplier introduction",
                    date_utc="2026-08-03 02:30:44",
                )
            ]
        )
        result = _tracker(db_conn, client, tmp_log).run()
        assert result.leads_created == 1
        lead = lead_mgr.find_by_email("purchaser@factory.com")
        assert lead.first_contacted_at == "2026-08-03 02:30:44"
        assert lead.last_contacted_at == "2026-08-03 02:30:44"

    def test_existing_lead_last_contacted_from_send_date(self, db_conn, tmp_log, lead_mgr):
        lead = lead_mgr.create(Lead(email="buyer@acme.com", source="manual", status="new"))
        client = FakeZohoClient(
            sent_messages=[
                _make_sent_msg(
                    to=["buyer@acme.com"],
                    subject="Follow-up Email From JinSixi",
                    date_utc="2026-08-04 01:00:07",
                )
            ],
            inbox_history=["buyer@acme.com"],
        )
        result = _tracker(db_conn, client, tmp_log).run()
        assert result.leads_updated == 1
        updated = lead_mgr.find_by_email("buyer@acme.com")
        assert updated.status == "contacted"  # promoted from new
        assert updated.first_contacted_at == "2026-08-04 01:00:07"
        assert updated.last_contacted_at == "2026-08-04 01:00:07"


class TestRecording:
    def test_new_lead_auto_created(self, db_conn, tmp_log, lead_mgr):
        client = FakeZohoClient(sent_messages=[_make_sent_msg(to=["new@company.com"])])
        result = _tracker(db_conn, client, tmp_log).run()
        assert result.leads_created == 1
        lead = lead_mgr.find_by_email("new@company.com")
        assert lead is not None
        assert lead.status == "contacted"
        assert lead.source == "zoho"
        assert NEW_MARKER in (lead.notes or "")

    def test_existing_lead_updates_last_contacted_no_status_downgrade(self, db_conn, tmp_log, lead_mgr):
        lead = lead_mgr.create(Lead(email="buyer@acme.com", source="manual", status="new"))
        lead_mgr.mark_replied(lead.id)
        client = FakeZohoClient(
            sent_messages=[_make_sent_msg(to=["buyer@acme.com"])],
            inbox_history=["buyer@acme.com"],  # 老客户
        )
        result = _tracker(db_conn, client, tmp_log).run()
        assert result.leads_created == 0
        assert result.leads_updated == 1
        updated = lead_mgr.find_by_email("buyer@acme.com")
        assert updated.status == "replied"  # not downgraded
        assert updated.last_contacted_at is not None

    def test_comm_row_fields(self, db_conn, tmp_log):
        client = FakeZohoClient(sent_messages=[_make_sent_msg(to=["buyer@acme.com"])])
        _tracker(db_conn, client, tmp_log).run()
        row = db_conn.execute("SELECT * FROM communications").fetchone()
        assert row["direction"] == "outbound"
        assert row["channel"] == "email"
        assert row["status"] == "sent"
        assert row["source"] == "zoho"
        assert row["message_id"] == "real-msg@zoho.com"
        assert row["template_id"] is None
        assert row["sent_at"] == "2026-08-04 10:00:00"  # Zoho Date header preserved

    def test_real_message_id_for_threading(self, db_conn, tmp_log, comm_mgr):
        client = FakeZohoClient(sent_messages=[_make_sent_msg(to=["buyer@acme.com"])])
        _tracker(db_conn, client, tmp_log).run()
        found = comm_mgr.get_by_message_id("real-msg@zoho.com")
        assert found is not None
        assert found["lead_id"] is not None

    def test_dedup_no_double_insert_on_rerun(self, db_conn, tmp_log):
        client = FakeZohoClient(sent_messages=[_make_sent_msg(to=["buyer@acme.com"])])
        tracker = _tracker(db_conn, client, tmp_log)
        first = tracker.run()
        assert first.comms_recorded == 1
        second = tracker.run()
        assert second.deduped == 1
        assert second.comms_recorded == 0
        assert db_conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 1
        assert db_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1

    def test_notes_append_idempotent(self, db_conn, tmp_log, lead_mgr):
        lead_mgr.create(
            Lead(email="new@company.com", source="zoho", status="contacted", notes=NEW_MARKER)
        )
        client = FakeZohoClient(sent_messages=[_make_sent_msg(to=["new@company.com"])])
        _tracker(db_conn, client, tmp_log).run()
        updated = lead_mgr.find_by_email("new@company.com")
        assert (updated.notes or "").count(NEW_MARKER) == 1

    def test_multiple_recipients_one_message(self, db_conn, tmp_log):
        msg = _make_sent_msg(to=["a@one.com", "b@two.com"])
        client = FakeZohoClient(sent_messages=[msg])
        result = _tracker(db_conn, client, tmp_log).run()
        assert result.comms_recorded == 2
        assert db_conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 2
        assert db_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2


class TestReport:
    def test_report_generation(self, db_conn, tmp_log):
        client = FakeZohoClient(
            sent_messages=[
                _make_sent_msg(to=["veteran@acme.com"], subject="Follow-up A"),
                _make_sent_msg(to=["new@purchaser.com"], subject="Hello B"),
            ],
            inbox_history=["veteran@acme.com"],
        )
        result = _tracker(db_conn, client, tmp_log).run(report_only=True)
        assert result.report_path is not None
        report_file = Path(result.report_path)
        assert report_file.exists()
        text = report_file.read_text(encoding="utf-8")
        assert "老客户" in text and "新联系" in text
        assert "veteran@acme.com" in text
        assert "new@purchaser.com" in text

    def test_report_only_writes_nothing(self, db_conn, tmp_log):
        client = FakeZohoClient(sent_messages=[_make_sent_msg(to=["buyer@acme.com"])])
        result = _tracker(db_conn, client, tmp_log).run(report_only=True)
        assert result.leads_created == 0
        assert result.leads_updated == 0
        assert result.comms_recorded == 0
        assert db_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
        assert db_conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0] == 0
