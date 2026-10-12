"""
IMAP Inbox Monitor — detects customer replies and filters out system emails.

Phase 2 of FTA email system. Connects to IMAP inbox, classifies incoming
messages (reply vs bounce vs auto-reply), matches replies to sent emails
via Message-ID / In-Reply-To / subject, and records them in the DB.

Critical: 163.com (NetEase) and other providers send delivery failure
notifications that MUST NOT be classified as customer replies. This
module applies 7 layers of filtering to prevent false positives.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Optional

import config
from crm.manager import (
    BlacklistManager,
    CommunicationManager,
    CustomerManager,
    LeadManager,
)
from crm.models import Communication
from services.email_reputation.suppression_service import (
    SuppressionPersistenceError,
    SuppressionService,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants — bounce / auto-reply detection patterns
# ═══════════════════════════════════════════════════════════════════════════

# Sender addresses that are NEVER customer replies
BOUNCE_FROM_PATTERNS = [
    "mailer-daemon", "postmaster",
    "mail-daemon", "mailer",
]
BOUNCE_FROM_DOMAINS = [
    "bounce.", "returns.", "noreply.", "no-reply.",
]

# Subject patterns for bounce/delivery-failure emails (Chinese + English)
BOUNCE_SUBJECT_KEYWORDS = [
    # English
    "undelivered mail", "returned mail", "delivery status notification",
    "failure notice", "mail delivery failed", "mail system error",
    "returned to sender", "delivery failure", "postmaster notify",
    "undeliverable", "message undeliverable", "warning: could not send",
    "delayed delivery", "mail delivery subsystem",
    "undeliverable mail", "delivery notification: delivery has failed",
    "rejected:", "unknown address", "recipient not found",
    # Common localized DSN subjects observed in the production 163 inbox.
    "nedostavljivo", "non remis", "unzustellbar", "nieznany adres",
    "problema ao entregar", "retorno ao remetente",
    # Chinese (163.com, QQ, etc.)
    "退信", "邮件投递失败", "邮件退回", "发送失败",
    "无法送达", "系统退信", "退信通知", "邮件被退回",
    "邮件未送达", "投递失败", "邮箱已满", "邮箱不存在",
]

# Auto-reply / out-of-office subject keywords (separate list)
AUTO_REPLY_SUBJECT_KEYWORDS = [
    "out of office", "auto reply", "automatic reply",
    "away from", "on vacation", "out of the office", "auto:",
    "自动回复", "自动答复",
]

# Automated/security messages are neither replies nor bounces.
SYSTEM_SUBJECT_KEYWORDS = [
    "requires verification", "verification verify", "security notification",
    "remote login reminder", "[spam]", "newsletter", "welcome to",
    "需要验证", "安全通知", "异地登录提醒",
]

# Combined list for the single-pass check
_ALL_SUBJECT_KEYWORDS = BOUNCE_SUBJECT_KEYWORDS + AUTO_REPLY_SUBJECT_KEYWORDS

# Body keywords indicating bounce (checked in first 500 chars)
BOUNCE_BODY_KEYWORDS = [
    "the following address failed", "user unknown",
    "mailbox not found", "does not exist", "recipient rejected",
    "address rejected", "mailbox unavailable", "invalid recipient",
    "no such user", "550", "551", "552", "553", "554",
    "unrouteable address", "account disabled",
    # Chinese
    "无法送达", "邮箱不存在", "收件人不存在",
    "邮件被拒绝", "地址不存在", "账号已停用",
    "投递失败", "收件人邮件地址不存在",
]

# Subject prefix patterns for actual replies (to strip for matching)
_REPLY_PREFIX_PATTERN = re.compile(
    r'(Re|RE|回复|答复|AW|Aw|Fwd|FWD|转发)\s*:\s*', re.IGNORECASE
)


def _strip_reply_prefixes(subject: str) -> str:
    """Strip all reply/fwd prefixes from a subject line.

    Handles multiple levels: 'Re: AW: Hello' → 'Hello'.
    Guards against infinite loops (max 10 iterations).
    """
    stripped = subject.strip()
    for _ in range(10):
        new_stripped = _REPLY_PREFIX_PATTERN.sub("", stripped, count=1).strip()
        # Some gateways insert markers between nested reply prefixes, e.g.
        # "Re: [DCE] Re: Fwd: Purchase of Fasteners".
        new_stripped = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", new_stripped).strip()
        if new_stripped == stripped:
            break
        stripped = new_stripped
    return stripped


def classify_persisted_inbound(subject: Optional[str], body: Optional[str]) -> "MessageClass":
    """Classify a stored inbound row using fields available in SQLite.

    Persisted legacy rows do not retain all RFC headers, so reply detection is
    deliberately strict: a human reply needs a recognized reply prefix. Bounce
    detection is deliberately broad to avoid counting provider DSNs as leads.
    """
    subject_lower = (subject or "").strip().lower()
    body_lower = (body or "")[:1000].lower()
    if any(keyword in subject_lower for keyword in AUTO_REPLY_SUBJECT_KEYWORDS):
        return MessageClass.AUTO_REPLY
    if any(keyword in subject_lower for keyword in BOUNCE_SUBJECT_KEYWORDS):
        return MessageClass.BOUNCE
    if any(keyword in body_lower for keyword in BOUNCE_BODY_KEYWORDS):
        return MessageClass.BOUNCE
    if any(keyword in subject_lower for keyword in SYSTEM_SUBJECT_KEYWORDS):
        return MessageClass.UNKNOWN
    if _REPLY_PREFIX_PATTERN.match((subject or "").strip()):
        return MessageClass.REPLY
    return MessageClass.UNKNOWN


# ═══════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════


class MessageClass(Enum):
    REPLY = "reply"
    BOUNCE = "bounce"
    AUTO_REPLY = "auto_reply"
    UNKNOWN = "unknown"


@dataclass
class ReplyMatch:
    """A matched customer reply with all relevant data."""
    from_email: str
    from_name: Optional[str] = None
    subject: Optional[str] = None
    body_preview: Optional[str] = None
    received_at: Optional[str] = None
    matched_comm_id: Optional[int] = None   # ID of the outbound communication
    lead_id: Optional[int] = None
    customer_id: Optional[int] = None
    match_method: str = ""                    # "message_id", "in_reply_to", "subject", "email"

    def to_dict(self) -> dict:
        return {
            "from_email": self.from_email,
            "from_name": self.from_name,
            "subject": self.subject,
            "body_preview": self.body_preview,
            "received_at": self.received_at,
            "matched_comm_id": self.matched_comm_id,
            "lead_id": self.lead_id,
            "customer_id": self.customer_id,
            "match_method": self.match_method,
        }


@dataclass
class InboxCheckResult:
    """Result of an inbox check operation."""
    checked: int = 0           # Total messages examined
    replies: list[ReplyMatch] = field(default_factory=list)
    bounces: int = 0           # Count of skipped bounce notifications
    auto_replies: int = 0      # Count of skipped auto-replies
    unknown: int = 0           # Count of unclassified messages
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "replies": [r.to_dict() for r in self.replies],
            "bounces": self.bounces,
            "auto_replies": self.auto_replies,
            "unknown": self.unknown,
            "errors": self.errors,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Header decoding helpers
# ═══════════════════════════════════════════════════════════════════════════


def _decode_header_value(header_value: Optional[str]) -> str:
    """Decode RFC 2047 encoded header value (e.g. =?UTF-8?B?...?=)."""
    if not header_value:
        return ""
    try:
        parts = decode_header(header_value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(part)
        return " ".join(decoded)
    except Exception:
        return str(header_value)


def _dsn_sent_at(msg) -> Optional[str]:
    """Return the DSN's Date header as UTC '%Y-%m-%d %H:%M:%S' (SQLite).

    Bounces are backfilled from history and stored with the date they were
    actually generated, not the scan time, so CRM "今日退信" attributes each
    DSN to the right day. Falls back to None (→ now) when unparseable.
    """
    raw = str(msg.get("Date", ""))
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _extract_email_address(header_value: Optional[str]) -> str:
    """Extract bare email address from 'Name <email>' format."""
    if not header_value:
        return ""
    match = re.search(r'<([^>]+)>', str(header_value))
    if match:
        return match.group(1).strip().lower()
    # If no angle brackets, return as-is (might already be bare email)
    return str(header_value).strip().lower()


# ═══════════════════════════════════════════════════════════════════════════
# Failed-recipient extraction (for persisting inbox-side DSN bounces)
# ═══════════════════════════════════════════════════════════════════════════

# Diagnostic headers (RFC 3464 / de-facto) that name the recipient a DSN was
# generated for, in priority order. The first one present wins.
_FAILED_RECIPIENT_HEADERS = [
    "Final-Recipient",
    "Original-Recipient",
    "X-Failed-Recipients",
    "Failed-Recipient",
    "X-Original-To",
]

_RFC822_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _own_sender_addresses() -> set[str]:
    """Addresses that belong to us — never treat one as the bounced recipient."""
    addrs = {
        getattr(config, "SMTP_USERNAME", ""),
        getattr(config, "SMTP_FROM_EMAIL", ""),
        getattr(config, "IMAP_USERNAME", ""),
        getattr(config, "ZOHO_SMTP_USERNAME", ""),
        getattr(config, "ZOHO_SMTP_FROM_EMAIL", ""),
        getattr(config, "ZOHO_IMAP_USERNAME", ""),
    }
    return {a.strip().lower() for a in addrs if a and isinstance(a, str)}


def extract_failed_recipient(msg) -> tuple[Optional[str], bool]:
    """Return (recipient, reliable) from a bounce/DSN message.

    ``recipient`` is the original failed recipient address, or None when it
    cannot be identified. ``reliable`` is True only when the address came from
    a DSN diagnostic header (Final-Recipient / Original-Recipient /
    X-Failed-Recipients / Failed-Recipient / X-Original-To) — safe to
    auto-suppress. When only the body fallback matched, ``reliable`` is False:
    a misclassified message's body can contain unrelated addresses, so the
    caller should link/persist but NOT auto-blacklist on it.

    Priority:
      1. RFC diagnostic headers.
      2. The first address in the message body that is not one of our own
         sending addresses (163.cn DSN bodies name the failed recipient while
         also echoing our envelope sender).
    """
    # 1. Diagnostic headers.
    for header in _FAILED_RECIPIENT_HEADERS:
        value = str(msg.get(header, ""))
        if not value:
            continue
        # Handle the "rfc822;<addr>" and "<addr>" forms.
        value = re.sub(r"^\s*rfc822\s*;", "", value, flags=re.IGNORECASE).strip()
        match = _RFC822_EMAIL_RE.search(value)
        if match:
            candidate = match.group(0).lower()
            if candidate not in _own_sender_addresses():
                return candidate, True

    # 2. Body fallback — first address that is not one of our own.
    own = _own_sender_addresses()
    for match in _RFC822_EMAIL_RE.finditer(InboxMonitor._extract_body_text(msg)):
        candidate = match.group(0).lower()
        if candidate not in own:
            return candidate, False
    return None, False


def create_imap_connection(
    host: str, port: int, username: str, password: str,
) -> imaplib.IMAP4_SSL:
    """Open an IMAP4_SSL connection and log in.

    Shared by InboxMonitor, engine.diagnostics, and the Zoho mail client.
    Raises imaplib.IMAP4.error / ssl.SSLError on failure.
    """
    ssl_context = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl_context)
    conn.login(username, password)
    return conn


# ═══════════════════════════════════════════════════════════════════════════
# InboxMonitor
# ═══════════════════════════════════════════════════════════════════════════


class InboxMonitor:
    """Monitor IMAP inbox for customer replies.

    Usage:
        monitor = InboxMonitor(conn)
        result = monitor.check_inbox(hours_back=24)
        # result.replies → list of ReplyMatch
        # result.bounces → count of filtered-out bounce emails
    """

    def __init__(self, conn):
        """Initialize with a DB connection and IMAP config from config.py."""
        self.conn = conn
        self.comm_mgr = CommunicationManager(conn)
        self.lead_mgr = LeadManager(conn)
        self.cust_mgr = CustomerManager(conn)
        self.blacklist_mgr = BlacklistManager(conn)
        self.suppression_service = SuppressionService(conn)

        self.host = config.IMAP_HOST
        self.port = config.IMAP_PORT
        self.username = config.IMAP_USERNAME
        self.password = config.IMAP_PASSWORD

    # ── Main entry ──────────────────────────────────────────────────────

    def check_inbox(
        self, folder: str = "INBOX", hours_back: int = 24
    ) -> InboxCheckResult:
        """Check inbox for new replies since `hours_back` ago.

        Args:
            folder: IMAP folder to check (default: INBOX).
            hours_back: Only examine messages received in the last N hours.

        Returns:
            InboxCheckResult with classified and matched replies.
        """
        result = InboxCheckResult()

        if not self.username or not self.password:
            result.errors.append("IMAP credentials not configured")
            return result

        imap_conn = None
        try:
            imap_conn = self._connect()
            imap_conn.select(folder, readonly=True)

            since_date = (datetime.now() - timedelta(hours=hours_back)).strftime(
                "%d-%b-%Y"
            )
            status, message_ids = imap_conn.search(
                None, f'(SINCE "{since_date}")'
            )
            if status != "OK":
                result.errors.append(f"IMAP search failed: {status}")
                return result

            msg_ids = message_ids[0].split()
            result.checked = len(msg_ids)
            logger.info(
                "IMAP: %d messages in %s since %s",
                result.checked, folder, since_date,
            )

            for msg_id in msg_ids:
                try:
                    status, msg_data = imap_conn.fetch(msg_id, "(RFC822 UID)")
                    if status != "OK":
                        continue

                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)

                    # IMAP UID dedups ALL processed messages (replies, bounces
                    # and unmatched inbound) so re-checks never double-count.
                    imap_uid = (
                        msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                    )

                    # ── Classify ──────────────────────────────────
                    classification = self._classify_message(msg)

                    if classification == MessageClass.BOUNCE:
                        # Persist the DSN (status='bounced') so the CRM dashboard
                        # shows it, the failed recipient is suppressed, and the
                        # message is not re-processed on the next check.
                        self._record_bounce(msg, imap_uid)
                        result.bounces += 1
                        logger.debug(
                            "IMAP: filtered bounce — from=%s subject=%s",
                            msg.get("From", "")[:80],
                            _decode_header_value(msg.get("Subject", ""))[:80],
                        )
                        continue

                    if classification == MessageClass.AUTO_REPLY:
                        result.auto_replies += 1
                        logger.debug(
                            "IMAP: filtered auto-reply — from=%s",
                            msg.get("From", "")[:80],
                        )
                        continue

                    if classification == MessageClass.UNKNOWN:
                        result.unknown += 1
                        continue

                    # ── Match to sent email ────────────────────────
                    match = self._match_to_sent(msg)
                    if match:
                        self._record_reply(match)
                        result.replies.append(match)
                        logger.info(
                            "IMAP: reply matched — from=%s lead=%s method=%s",
                            match.from_email, match.lead_id, match.match_method,
                        )

                    # Store IMAP UID to prevent re-processing
                    if not self.comm_mgr.imap_uid_exists(imap_uid):
                        # Record that we've seen this message (even if unmatched)
                        rec = Communication(
                            direction="inbound",
                            channel="email",
                            status="received",
                            subject=_decode_header_value(msg.get("Subject", "")),
                            imap_uid=imap_uid,
                        )
                        self.comm_mgr.log(rec)

                except Exception as exc:
                    logger.warning("IMAP: error processing message: %s", exc)

            logger.info(
                "IMAP check complete: checked=%d replies=%d "
                "bounces=%d auto_replies=%d unknown=%d errors=%d",
                result.checked, len(result.replies), result.bounces,
                result.auto_replies, result.unknown, len(result.errors),
            )

        except imaplib.IMAP4.error as e:
            result.errors.append(f"IMAP protocol error: {e}")
            logger.error("IMAP protocol error: %s", e)
        except ssl.SSLError as e:
            result.errors.append(f"SSL error: {e}")
            logger.error("IMAP SSL error: %s", e)
        except Exception as e:
            result.errors.append(f"Unexpected error: {e}")
            logger.exception("IMAP unexpected error")
        finally:
            if imap_conn:
                try:
                    imap_conn.close()
                    imap_conn.logout()
                except Exception:
                    pass

        return result

    # ── IMAP connection ─────────────────────────────────────────────────

    def _connect(self) -> imaplib.IMAP4_SSL:
        """Establish IMAP SSL connection."""
        logger.info("Connecting to IMAP: %s:%d", self.host, self.port)
        conn = create_imap_connection(self.host, self.port, self.username, self.password)
        logger.info("IMAP login successful")
        return conn

    # ── Message classification (the 7-layer filter) ─────────────────────

    def _classify_message(self, msg: email.message.Message) -> MessageClass:
        """Classify an incoming email: reply, bounce, auto_reply, or unknown.

        Applies 7 layers of filtering. Returns REPLY only if ALL filters pass.
        Layer order is critical — cheapest/fastest checks first.
        """
        from_header = str(msg.get("From", ""))
        subject = _decode_header_value(msg.get("Subject", ""))
        to_header = str(msg.get("To", ""))
        return_path = str(msg.get("Return-Path", ""))
        auto_submitted = str(msg.get("Auto-Submitted", ""))
        precedence = str(msg.get("Precedence", ""))

        # ── Layer 1: From address (mailer-daemon / postmaster) ───────
        from_lower = from_header.lower()
        for pattern in BOUNCE_FROM_PATTERNS:
            if pattern in from_lower:
                return MessageClass.BOUNCE
        for domain in BOUNCE_FROM_DOMAINS:
            if domain in from_lower:
                return MessageClass.BOUNCE

        # ── Layer 2: Auto-Submitted header (RFC 3834) ────────────────
        if auto_submitted.lower() in ("auto-replied", "auto-generated"):
            # Some DSNs (observed: Trend Micro / tmes.trendmicro.*) set
            # Auto-Submitted: auto-replied on real delivery-failure notices.
            # An explicit bounce subject must win over the Auto-Submitted
            # signal, or a hard bounce is silently counted as an auto-reply
            # and never reaches the CRM (今日退信).
            if not any(kw in subject.lower() for kw in BOUNCE_SUBJECT_KEYWORDS):
                return MessageClass.AUTO_REPLY

        # ── Layer 3: Subject keyword matching ────────────────────────
        subject_lower = subject.lower()
        # Check auto-reply keywords FIRST (more specific)
        for kw in AUTO_REPLY_SUBJECT_KEYWORDS:
            if kw in subject_lower:
                return MessageClass.AUTO_REPLY
        # Then check bounce keywords
        for kw in BOUNCE_SUBJECT_KEYWORDS:
            if kw in subject_lower:
                return MessageClass.BOUNCE

        # ── Layer 4: Precedence header ───────────────────────────────
        if precedence.lower() in ("bulk", "junk", "list"):
            return MessageClass.UNKNOWN

        # ── Layer 5: Return-Path (empty = bounce) ────────────────────
        if return_path.strip() in ("", "<>"):
            return MessageClass.BOUNCE

        # ── Layer 6: To address — must be to us (lenient) ────────────
        our_addresses = [
            self.username.lower() if self.username else "",
            config.SMTP_USERNAME.lower() if config.SMTP_USERNAME else "",
            config.SMTP_FROM_EMAIL.lower() if config.SMTP_FROM_EMAIL else "",
        ]
        our_addresses = [a for a in our_addresses if a]  # filter empties
        to_lower = to_header.lower()
        if our_addresses and to_lower:
            is_to_us = any(addr in to_lower for addr in our_addresses)
            if not is_to_us:
                return MessageClass.UNKNOWN

        # ── Layer 7: Body content check ──────────────────────────────
        body = self._extract_body_text(msg)
        body_lower = body[:500].lower()
        for kw in BOUNCE_BODY_KEYWORDS:
            if kw in body_lower:
                return MessageClass.BOUNCE

        # A genuine reply should carry RFC thread headers or a recognized
        # reply prefix. Provider notifications without either signal are not
        # allowed to fall through as customer replies.
        has_thread_header = bool(msg.get("In-Reply-To") or msg.get("References"))
        has_reply_prefix = bool(_REPLY_PREFIX_PATTERN.match(subject.strip()))
        if not has_thread_header and not has_reply_prefix:
            return MessageClass.UNKNOWN

        # ── All filters passed — this is a genuine reply ─────────────
        return MessageClass.REPLY

    # ── Reply matching ─────────────────────────────────────────────────

    def _match_to_sent(self, msg: email.message.Message) -> Optional[ReplyMatch]:
        """Match an incoming reply to a sent email in communications table.

        Tries, in priority order:
        1. In-Reply-To header → match communications.message_id
        2. References header → match last message_id in chain
        3. Subject matching (strip Re:/回复: prefix)
        4. Sender email matching (fallback: most recent outbound to this address)
        """
        from_email = _extract_email_address(msg.get("From", ""))
        from_name = _decode_header_value(msg.get("From", ""))
        # Extract display name: "Lucas <lucas@x.com>" → "Lucas"
        if "<" in from_name:
            from_name = from_name.split("<")[0].strip().strip('"')
        subject_raw = _decode_header_value(msg.get("Subject", ""))
        received_at = str(msg.get("Date", ""))

        match = ReplyMatch(
            from_email=from_email,
            from_name=from_name,
            subject=subject_raw,
            body_preview=self._extract_body_text(msg)[:300],
            received_at=received_at,
        )

        # Priority 1: In-Reply-To header
        in_reply_to = str(msg.get("In-Reply-To", ""))
        if in_reply_to:
            # Extract Message-ID (may have angle brackets)
            msg_id_match = re.search(r'<([^>]+)>', in_reply_to)
            if msg_id_match:
                msg_id = msg_id_match.group(1)
                outbound = self.comm_mgr.get_by_message_id(msg_id)
                if outbound:
                    match.matched_comm_id = outbound["id"]
                    match.lead_id = outbound["lead_id"]
                    match.customer_id = outbound["customer_id"]
                    match.match_method = "message_id"
                    return match

        # Priority 2: References header (last entry in chain)
        references = str(msg.get("References", ""))
        if references:
            ref_ids = re.findall(r'<([^>]+)>', references)
            for ref_id in reversed(ref_ids):
                outbound = self.comm_mgr.get_by_message_id(ref_id)
                if outbound:
                    match.matched_comm_id = outbound["id"]
                    match.lead_id = outbound["lead_id"]
                    match.customer_id = outbound["customer_id"]
                    match.match_method = "references"
                    return match

        # Priority 3: Subject matching
        clean_subject = _strip_reply_prefixes(subject_raw)
        if clean_subject and from_email:
            # Search communications for matching subject + recipient
            outbound = self.comm_mgr.get_latest_outbound_to(from_email)
            if outbound:
                outbound_subject = (outbound.get("subject") or "").strip()
                # Compare cleaned subjects (case-insensitive)
                if clean_subject.lower() == outbound_subject.lower():
                    match.matched_comm_id = outbound["id"]
                    match.lead_id = outbound["lead_id"]
                    match.customer_id = outbound["customer_id"]
                    match.match_method = "subject"
                    return match

        # Priority 4: Email-only fallback (most recent outbound)
        if from_email:
            outbound = self.comm_mgr.get_latest_outbound_to(from_email)
            if outbound:
                match.matched_comm_id = outbound["id"]
                match.lead_id = outbound["lead_id"]
                match.customer_id = outbound["customer_id"]
                match.match_method = "email"
                return match

        return match if match.matched_comm_id else None

    # ── Record reply in DB ──────────────────────────────────────────────

    def _record_reply(self, match: ReplyMatch) -> None:
        """Record a matched reply in the database.

        Actions:
        1. Insert new inbound Communication record
        2. Update outbound Communication status to 'replied'
        3. Update lead status to 'replied'
        4. Update customer last_contacted if applicable
        """
        # 1. Log inbound message
        # in_reply_to: reference the matched outbound email's real Message-ID
        in_reply_to = None
        if match.matched_comm_id:
            row = self.conn.execute(
                "SELECT message_id FROM communications WHERE id = ?",
                (match.matched_comm_id,),
            ).fetchone()
            if row:
                in_reply_to = row["message_id"]

        inbound = Communication(
            lead_id=match.lead_id,
            customer_id=match.customer_id,
            channel="email",
            direction="inbound",
            subject=match.subject,
            body=match.body_preview,
            status="received",
            in_reply_to=in_reply_to,
        )
        inbound = self.comm_mgr.log(inbound)

        # 2. Mark the outbound communication as replied
        if match.matched_comm_id:
            self.comm_mgr.mark_replied(match.matched_comm_id)

        # 3. Update lead status
        if match.lead_id:
            try:
                self.lead_mgr.mark_replied(match.lead_id)
            except Exception:
                logger.warning(
                    "Failed to mark lead #%d as replied", match.lead_id
                )

        # 4. Update customer if applicable
        if match.customer_id:
            try:
                self.cust_mgr.record_contact(match.customer_id)
            except Exception:
                logger.warning(
                    "Failed to record contact for customer #%d",
                    match.customer_id,
                )

        logger.info(
            "Reply recorded: from=%s lead=%d method=%s",
            match.from_email, match.lead_id, match.match_method,
        )

    def _record_bounce(self, msg, imap_uid: str) -> None:
        """Persist an inbox-side DSN/bounce into the CRM.

        Previously bounces were counted and discarded, so async DSNs from the
        163 mailbox never appeared in the dashboard ("今日退信" only saw
        synchronous SMTP hard-bounces). This records the DSN as an inbound
        communication with status='bounced', links the failed recipient to its
        lead when known, and suppresses the recipient so we stop sending to a
        confirmed-bad address. Dedup via imap_uid, same as replies.
        """
        if self.comm_mgr.imap_uid_exists(imap_uid):
            return
        subject = _decode_header_value(msg.get("Subject", ""))
        body = self._extract_body_text(msg)[:2000]
        recipient, recipient_reliable = extract_failed_recipient(msg)

        lead_id = None
        if recipient:
            lead = self.lead_mgr.find_by_email(recipient)
            if lead:
                lead_id = lead.id

        inbound = Communication(
            channel="email",
            direction="inbound",
            status="bounced",
            subject=subject,
            body=body,
            lead_id=lead_id,
            imap_uid=imap_uid,
            sent_at=_dsn_sent_at(msg),
        )
        self.comm_mgr.log(inbound)

        # A DSN diagnostic header proves the recipient identity, but does not
        # prove that the failure is a permanent recipient hard bounce. Reuse
        # P2/P3's parser and classifier so policy, reputation, content and
        # mailbox-full DSNs remain recorded but do not suppress the recipient.
        # A body-only match remains too weak even to attempt suppression.
        if recipient and recipient_reliable:
            try:
                record = self.suppression_service.suppress_from_smtp_error(
                    recipient,
                    "\n".join(part for part in (subject, body) if part),
                    provider="default",
                )
                if record is not None:
                    logger.info("IMAP: confirmed hard bounce suppressed")
            except SuppressionPersistenceError:
                # P3's fail-closed compatibility fallback: this exception is
                # raised only after the diagnostic was classified as a
                # confirmed recipient hard bounce.
                self.blacklist_mgr.add(recipient, reason="hard_bounce_fallback")
                logger.warning(
                    "Canonical IMAP hard-bounce suppression failed; "
                    "legacy fail-closed fallback installed"
                )
            except ValueError:
                # A permissive DSN header regex can still produce an address
                # rejected by the strict P2 parser. The DSN was already
                # persisted above, but an unclassifiable address must neither
                # abort inbox processing nor enter legacy fallback suppression.
                logger.warning(
                    "IMAP: bounce recipient could not be classified; "
                    "kept recorded without suppression"
                )

        logger.info(
            "IMAP: recorded bounce uid=%s subject=%s recipient=%s lead=%s",
            imap_uid, subject[:60] or "?", recipient or "?", lead_id,
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _extract_body_text(msg: email.message.Message) -> str:
        """Extract plain text body from an email message."""
        body_parts = []
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            body_parts.append(payload.decode(charset, errors="replace"))
                    except Exception:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    body_parts.append(payload.decode(charset, errors="replace"))
            except Exception:
                pass
        return "\n".join(body_parts)


# ═══════════════════════════════════════════════════════════════════════════
# Convenience function
# ═══════════════════════════════════════════════════════════════════════════


def check_inbox_now(conn, hours_back: int = 24) -> InboxCheckResult:
    """Synchronous convenience wrapper for checking inbox.

    Args:
        conn: SQLite database connection.
        hours_back: Look back N hours for new messages.

    Returns:
        InboxCheckResult.
    """
    monitor = InboxMonitor(conn)
    return monitor.check_inbox(hours_back=hours_back)
