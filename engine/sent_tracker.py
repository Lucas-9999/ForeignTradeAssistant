"""
Zoho Sent-Follow-Up Tracker — detects the user's manual follow-up emails
sent from the personal Zoho mailbox, classifies recipients, records them
into the CRM, and generates a daily Markdown report.

Zoho is personal communication only — this tracker NEVER sends email and
does NOT touch the NetEase batch/follow-up pipeline.

Flow: ZohoMailClient (IMAP) → classify (Zoho Inbox has history FROM the
recipient ⇒ 老客户/询过价, else 新联系) → find-or-create lead → record
communication → Markdown report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import config
from crm.manager import CommunicationManager, LeadManager
from crm.models import Communication, Lead
from emailer.zoho import ZohoMailClient, ZohoSentMessage

logger = logging.getLogger(__name__)

NEW_CONTACT_NOTE = "主动建立联系 (Zoho 人工跟进)"
VETERAN_FOLLOWUP_NOTE = "Zoho 人工跟进 (老客户/询过价)"

# Subject-line markers the user consistently uses to tell the two buckets apart:
#   "Follow-up Email From JinSixi"          → 老客户 (had email exchanges before)
#   "Follow-up on Our Company Information"  → 老客户 (exchanged in the 163 企业邮箱,
#                                              possibly forwarded by the supervisor)
#   "Fastener supplier introduction"        → 新联系 (cold outreach to new prospects)
VETERAN_SUBJECT_MARKERS = (
    "follow-up email from jinsixi",
    "follow-up on our company information",
)
NEW_CONTACT_SUBJECT_MARKERS = ("fastener supplier introduction",)


class RecipientClass(str, Enum):
    VETERAN = "veteran"  # 老客户 — 询过价: subject marker OR Zoho Inbox has mail FROM them
    NEW = "new"          # 新联系 — subject marker OR no history in Zoho Inbox


class ClassifyBasis(str, Enum):
    SUBJECT = "subject"        # subject-line marker decided it
    INBOX_HISTORY = "inbox"    # no subject marker; Zoho Inbox history decided it
    DEFAULT = "default"        # neither — treated as 新联系


@dataclass
class TrackedRecipient:
    """One recipient of a detected sent email, with its classification."""

    email: str
    classification: RecipientClass
    basis: ClassifyBasis
    subject: str
    message_id: str
    date_utc: str = ""
    lead_id: Optional[int] = None
    company_name: Optional[str] = None
    recorded: bool = False


@dataclass
class SentTrackerResult:
    scanned: int = 0
    recipients_total: int = 0
    veterans: list[TrackedRecipient] = field(default_factory=list)
    new_contacts: list[TrackedRecipient] = field(default_factory=list)
    leads_created: int = 0
    leads_updated: int = 0
    comms_recorded: int = 0
    deduped: int = 0
    errors: list[str] = field(default_factory=list)
    report_path: Optional[str] = None
    report_only: bool = False


class ZohoSentTracker:
    """Detect + classify + record + report the user's Zoho follow-ups.

    Args:
        conn: sqlite3.Connection with row_factory set (crm.db.get_connection).
        client: ZohoMailClient (injectable for tests).
        log_dir: report output dir (defaults to config.LOG_DIR).
    """

    def __init__(
        self,
        conn,
        client: Optional[ZohoMailClient] = None,
        log_dir: Optional[Path] = None,
    ):
        self.conn = conn
        self.client = client or ZohoMailClient()
        self.log_dir = Path(log_dir) if log_dir else config.LOG_DIR
        self.lead_mgr = LeadManager(conn)
        self.comm_mgr = CommunicationManager(conn)

    # ── Entry point ─────────────────────────────────────────────────────

    def run(
        self,
        days: Optional[int] = None,
        report_only: bool = False,
        write_report: bool = True,
    ) -> SentTrackerResult:
        """Scan Zoho Sent, classify + record recipients, and report.

        Never raises out of here for scheduler safety — all failures are
        collected into result.errors.
        """
        result = SentTrackerResult(report_only=report_only)
        days = days if days is not None else config.ZOHO_SENT_WINDOW_DAYS

        if not self.client.has_credentials:
            msg = (
                "Zoho IMAP credentials not configured "
                "(FTA_ZOHO_IMAP_USERNAME / FTA_ZOHO_IMAP_PASSWORD)"
            )
            result.errors.append(msg)
            logger.error(msg)
            return result

        scanned_ok = False
        try:
            self.client.connect()
            messages = self.client.scan_sent(
                days=days, max_messages=config.ZOHO_SENT_MAX_EMAILS
            )
            result.scanned = len(messages)
            scanned_ok = True
            for msg in messages:
                self._process_message(msg, result, report_only)
        except Exception as e:
            result.errors.append(f"Zoho sent scan failed: {type(e).__name__}: {e}")
            logger.exception("Zoho sent scan failed")
        finally:
            self.client.close()

        if scanned_ok and write_report:
            self._write_report(result, days)
        self._print_summary(result, days)
        return result

    # ── Per-message processing ──────────────────────────────────────────

    def _process_message(
        self, msg: ZohoSentMessage, result: SentTrackerResult, report_only: bool
    ) -> None:
        for email_addr in msg.recipients:
            result.recipients_total += 1
            if self._is_duplicate(msg.message_id, email_addr):
                result.deduped += 1
                continue
            rec = self._classify(msg, email_addr)
            if not report_only:
                self._record(rec, msg, result)
            if rec.classification == RecipientClass.VETERAN:
                result.veterans.append(rec)
            else:
                result.new_contacts.append(rec)

    def _classify(self, msg: ZohoSentMessage, email_addr: str) -> TrackedRecipient:
        """Subject-first classification, inbox history as fallback.

        The user's two subject conventions decide the bucket deterministically:
          * "Follow-up Email From JinSixi"   → 老客户/询过价
          * "Fastener supplier introduction" → 新联系 (cold outreach)
        If neither marker matches, fall back to Zoho Inbox history (has mail
        FROM the recipient ⇒ 老客户, else 新联系).
        """
        subject = (msg.subject or "").lower()
        for marker in VETERAN_SUBJECT_MARKERS:
            if marker in subject:
                return self._build_recipient(email_addr, msg, RecipientClass.VETERAN, ClassifyBasis.SUBJECT)
        for marker in NEW_CONTACT_SUBJECT_MARKERS:
            if marker in subject:
                return self._build_recipient(email_addr, msg, RecipientClass.NEW, ClassifyBasis.SUBJECT)
        has_history = self.client.inbox_has_from(email_addr)
        if has_history:
            return self._build_recipient(email_addr, msg, RecipientClass.VETERAN, ClassifyBasis.INBOX_HISTORY)
        return self._build_recipient(email_addr, msg, RecipientClass.NEW, ClassifyBasis.DEFAULT)

    def _build_recipient(
        self, email_addr: str, msg: ZohoSentMessage, cls: RecipientClass, basis: ClassifyBasis
    ) -> TrackedRecipient:
        lead = self.lead_mgr.find_by_email(email_addr)
        return TrackedRecipient(
            email=email_addr,
            classification=cls,
            basis=basis,
            subject=msg.subject,
            message_id=msg.message_id,
            date_utc=msg.date_utc,
            lead_id=lead.id if lead else None,
            company_name=lead.company_name if lead else None,
        )

    def _is_duplicate(self, message_id: str, email_addr: str) -> bool:
        """Dedup key is (message_id, recipient email) — one sent mail, N recipients."""
        if not message_id:
            return False
        row = self.conn.execute(
            """SELECT 1 FROM communications c
               JOIN leads l ON l.id = c.lead_id
               WHERE c.message_id = ? AND LOWER(l.email) = LOWER(?) AND c.source = 'zoho'
               LIMIT 1""",
            (message_id, email_addr),
        ).fetchone()
        return row is not None

    def _record(
        self, rec: TrackedRecipient, msg: ZohoSentMessage, result: SentTrackerResult
    ) -> None:
        """Find-or-create lead, bump contact timestamps, log the outbound comm.

        first/last_contacted_at are set from the actual Zoho send time
        (rec.date_utc) so the CRM reflects when the follow-up really happened,
        not when the scan ran.
        """
        contacted_at = rec.date_utc or None
        if rec.lead_id:
            self.lead_mgr.record_manual_contact(rec.lead_id, contacted_at=contacted_at)
            result.leads_updated += 1
            if rec.classification == RecipientClass.NEW:
                lead = self.lead_mgr.find_by_email(rec.email)
                if lead:
                    self._append_contact_note(lead, NEW_CONTACT_NOTE)
        else:
            note = (
                NEW_CONTACT_NOTE
                if rec.classification == RecipientClass.NEW
                else VETERAN_FOLLOWUP_NOTE
            )
            created = self.lead_mgr.create(
                Lead(
                    email=rec.email,
                    source="zoho",
                    status="contacted",
                    notes=note,
                )
            )
            rec.lead_id = created.id
            result.leads_created += 1
            self.lead_mgr.record_manual_contact(created.id, contacted_at=contacted_at)

        self.comm_mgr.log(
            Communication(
                lead_id=rec.lead_id,
                channel="email",
                direction="outbound",
                subject=rec.subject,
                body=msg.body_preview,
                status="sent",
                message_id=rec.message_id,
                imap_uid=msg.imap_uid,
                source="zoho",
                sent_at=rec.date_utc or None,
            )
        )
        rec.recorded = True
        result.comms_recorded += 1

    def _append_contact_note(self, lead: Lead, note: str) -> None:
        """Idempotently append a marker note to an existing lead."""
        current = lead.notes or ""
        if note in current:
            return
        new_notes = (current.rstrip() + "\n" + note) if current else note
        self.conn.execute(
            "UPDATE leads SET notes = ?, updated_at = datetime('now') WHERE id = ?",
            (new_notes, lead.id),
        )
        self.conn.commit()

    # ── Report ──────────────────────────────────────────────────────────

    def _write_report(self, result: SentTrackerResult, days: int) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"sent_tracker_{today}.md"

        lines = [
            f"# Zoho 已发送跟进追踪 — {today}",
            "",
            f"- 扫描窗口: {days} 天 | 已发送邮件: {result.scanned} 封 | 收件人: {result.recipients_total}",
            f"- 模式: {'只读 dry-run，未写入数据库' if result.report_only else '已记录进 CRM'}",
            "",
            "## 老客户（询过价）",
            "",
            "| 公司 | 邮箱 | 主题 | 分类依据 |",
            "|---|---|---|---|",
        ]
        for r in result.veterans:
            lines.append(
                f"| {self._cell(r.company_name or '')} | {self._cell(r.email)} "
                f"| {self._cell(r.subject)} | {self._basis_label(r.basis)} |"
            )
        lines += [
            "",
            "## 新联系 — 正在尝试建立联系",
            "",
            "| 邮箱 | 主题 | 分类依据 |",
            "|---|---|---|",
        ]
        for r in result.new_contacts:
            lines.append(
                f"| {self._cell(r.email)} | {self._cell(r.subject)} | {self._basis_label(r.basis)} |"
            )
        lines += [
            "",
            "## 汇总",
            "",
            f"- 新建 lead: {result.leads_created} | 更新 lead: {result.leads_updated} "
            f"| 记录通讯: {result.comms_recorded} | 去重跳过: {result.deduped}",
        ]
        if result.errors:
            lines += ["", "### 错误"]
            lines += [f"- {e}" for e in result.errors]
        lines += [
            "",
            "> 注: Zoho 存档剥离 Bcc，仅 To/Cc 收件人可追踪。",
        ]

        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result.report_path = str(path)
        logger.info("Zoho sent report written: %s", path)

    @staticmethod
    def _cell(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    @staticmethod
    def _basis_label(basis: ClassifyBasis) -> str:
        return {
            ClassifyBasis.SUBJECT: "主题标记",
            ClassifyBasis.INBOX_HISTORY: "收件箱往来",
            ClassifyBasis.DEFAULT: "无历史",
        }[basis]

    def _print_summary(self, result: SentTrackerResult, days: int) -> None:
        print()
        print("=" * 60)
        print(f"  Zoho 已发送跟进追踪 ({'dry-run' if result.report_only else '记录'})")
        print("=" * 60)
        print(f"  窗口: {days} 天 | 扫描: {result.scanned} 封 | 收件人: {result.recipients_total}")
        print(f"  老客户(询过价): {len(result.veterans)}")
        for r in result.veterans:
            print(f"    - {r.company_name or r.email}  <{r.email}>  {r.subject[:60]}")
        print(f"  新联系: {len(result.new_contacts)}")
        for r in result.new_contacts:
            print(f"    - {r.email}  {r.subject[:60]}")
        print(
            f"  新建 lead: {result.leads_created} | 更新: {result.leads_updated} "
            f"| 记录通讯: {result.comms_recorded} | 去重: {result.deduped}"
        )
        if result.errors:
            print("  错误:")
            for e in result.errors:
                print(f"    ! {e}")
        if result.report_path:
            print(f"  报告: {result.report_path}")
        print("=" * 60)
