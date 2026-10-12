"""
Zoho IMAP client — scans the user's personal Zoho mailbox (Sent + Inbox).

Zoho (operator@example.com) is used ONLY for manual personal follow-ups.
Bulk/auto sends stay on the NetEase enterprise mailbox (FTA_SMTP_*/FTA_IMAP_*).
This module is a read-only IMAP client — it never sends email.

Used by engine.sent_tracker (detection/recording/report) and the CLI
(tools/track_zoho_sent.py).
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional

import config
from emailer.inbox import create_imap_connection, _decode_header_value

logger = logging.getLogger(__name__)

# Sent folder name candidates — mirrors engine/diagnostics.py SENT_FOLDER_CANDIDATES.
SENT_FOLDER_CANDIDATES = [
    "Sent",
    "Sent Items",
    "Sent Messages",
    "已发送",
    "Sent Mail",
    "INBOX.Sent",
    "INBOX.Sent Items",
    "[Gmail]/Sent Mail",
    '"Sent"',
    '"Sent Items"',
]


@dataclass
class ZohoSentMessage:
    """One outbound email found in the Zoho Sent folder."""

    message_id: str                    # real Zoho Message-ID (angle brackets stripped)
    subject: str
    date_utc: str                      # "YYYY-MM-DD HH:MM:SS" in UTC (from Date header)
    recipients: list[str]              # normalized lowercase To+Cc emails, self excluded
    to_emails: list[str] = field(default_factory=list)
    cc_emails: list[str] = field(default_factory=list)
    imap_uid: str = ""
    body_preview: str = ""


def _extract_body_preview(msg: email.message.Message, max_chars: int = 300) -> str:
    """Extract a short plain-text preview from an email message."""
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
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

    text = "\n".join(body_parts).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text


class ZohoMailClient:
    """Read-only IMAP client for the personal Zoho mailbox.

    Credentials default to config.ZOHO_IMAP_*. Can be overridden in the
    constructor (useful for tests). A single connection is opened lazily
    and reused until close().
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.host = host or config.ZOHO_IMAP_HOST
        self.port = port or config.ZOHO_IMAP_PORT
        self.username = username or config.ZOHO_IMAP_USERNAME
        self.password = password or config.ZOHO_IMAP_PASSWORD
        self.conn: Optional[imaplib.IMAP4_SSL] = None
        self._sent_folder: Optional[str] = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)

    # ── Connection ──────────────────────────────────────────────────────

    def connect(self) -> imaplib.IMAP4_SSL:
        """Open an IMAP4_SSL connection and log in. Raises ValueError if no creds."""
        if not self.has_credentials:
            raise ValueError(
                "Zoho IMAP credentials not configured "
                "(FTA_ZOHO_IMAP_USERNAME / FTA_ZOHO_IMAP_PASSWORD)"
            )
        self.conn = create_imap_connection(
            self.host, self.port, self.username, self.password
        )
        logger.info("Zoho IMAP connected: %s:%d", self.host, self.port)
        return self.conn

    def close(self) -> None:
        if self.conn:
            try:
                self.conn.close()
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def _require_conn(self) -> imaplib.IMAP4_SSL:
        if self.conn is None:
            self.connect()
        return self.conn

    # ── Sent folder detection ───────────────────────────────────────────

    def detect_sent_folder(self) -> Optional[str]:
        """Detect the Sent folder name (exact then fuzzy). Never hardcoded."""
        if self._sent_folder:
            return self._sent_folder
        conn = self._require_conn()
        status, listing = conn.list()
        if status != "OK":
            return None
        names = []
        for line in listing:
            decoded = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
            m = re.search(r'"([^"]+)"\s*$', decoded)
            names.append(m.group(1) if m else decoded.strip())

        for candidate in SENT_FOLDER_CANDIDATES:
            for name in names:
                if name.lower() == candidate.strip('"').lower():
                    self._sent_folder = name
                    logger.info("Zoho Sent folder detected: %r", name)
                    return name

        # Fuzzy fallback: last path segment starts with "sent"
        for name in names:
            last = name.lower().split("/")[-1].strip('"')
            if last.startswith("sent"):
                self._sent_folder = name
                logger.info("Zoho Sent folder detected (fuzzy): %r", name)
                return name
        return None

    # ── Sent scanning ───────────────────────────────────────────────────

    def scan_sent(self, days: int = 1, max_messages: int = 0) -> list[ZohoSentMessage]:
        """Scan the Sent folder for emails within the last `days` days.

        Filtering uses the parsed Date header (converted to UTC), not
        INTERNALDATE, to avoid timezone skew. Results newest-first, capped
        by max_messages (0 = no cap).
        """
        conn = self._require_conn()
        folder = self.detect_sent_folder()
        if not folder:
            logger.error("Zoho Sent folder not found")
            return []
        status, _ = conn.select(f'"{folder}"', readonly=True)
        if status != "OK":
            logger.error("Cannot select Zoho Sent folder: %s", folder)
            return []

        since = (datetime.now() - timedelta(days=days + 1)).strftime("%d-%b-%Y")
        status, message_ids = conn.search(None, f'(SINCE "{since}")')
        if status != "OK" or not message_ids or not message_ids[0]:
            return []

        ids = list(reversed(message_ids[0].split()))  # newest first
        if max_messages > 0:
            ids = ids[:max_messages]

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        results: list[ZohoSentMessage] = []
        for mid in ids:
            try:
                status, data = conn.fetch(mid, "(RFC822 UID)")
                if status != "OK" or not data or not data[0]:
                    continue
                uid = self._extract_uid(data[0])
                raw = data[0][1]
                msg = email.message_from_bytes(raw)
                parsed = self._parse_sent(msg, uid)
                if parsed is None:
                    continue
                sent_msg, date_dt = parsed
                if date_dt is not None and date_dt < cutoff:
                    continue
                results.append(sent_msg)
            except Exception as e:
                logger.warning("Zoho scan_sent error on message %s: %s", mid, e)
        logger.info("Zoho scan_sent: %d message(s) in window", len(results))
        return results

    @staticmethod
    def _extract_uid(fetch_item: object) -> str:
        # fetch_item is a tuple like (b'1 (RFC822 {1234} UID 5678)', b'<raw>')
        meta = fetch_item[0] if isinstance(fetch_item, tuple) else b""
        m = re.search(rb"UID (\d+)", meta)
        return m.group(1).decode() if m else ""

    def _parse_sent(
        self, msg: email.message.Message, uid: str
    ) -> Optional[tuple[ZohoSentMessage, Optional[datetime]]]:
        """Parse a sent message into ZohoSentMessage (+ parsed date for filtering)."""
        message_id = self._extract_message_id(msg) or uid
        if not message_id:
            return None
        subject = _decode_header_value(msg.get("Subject", ""))
        to_emails = [e.lower() for _, e in getaddresses([str(msg.get("To", ""))]) if e]
        cc_emails = [e.lower() for _, e in getaddresses([str(msg.get("Cc", ""))]) if e]

        self_addr = (self.username or "").lower()
        seen: set[str] = set()
        recipients: list[str] = []
        for e in to_emails + cc_emails:
            if e and e != self_addr and e not in seen:
                seen.add(e)
                recipients.append(e)

        date_dt: Optional[datetime] = None
        date_str = str(msg.get("Date", "")).strip()
        if date_str:
            try:
                dt = parsedate_to_datetime(date_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                date_dt = dt.astimezone(timezone.utc)
            except Exception:
                date_dt = None
        date_utc = date_dt.strftime("%Y-%m-%d %H:%M:%S") if date_dt else ""

        sent_msg = ZohoSentMessage(
            message_id=message_id,
            subject=subject,
            date_utc=date_utc,
            recipients=recipients,
            to_emails=to_emails,
            cc_emails=cc_emails,
            imap_uid=uid,
            body_preview=_extract_body_preview(msg, 300),
        )
        return sent_msg, date_dt

    @staticmethod
    def _extract_message_id(msg: email.message.Message) -> str:
        raw = str(msg.get("Message-ID", "")).strip()
        m = re.search(r"<([^>]+)>", raw)
        return m.group(1).strip() if m else raw

    # ── Inbox history lookup (询过价 classification) ─────────────────────

    def inbox_has_from(self, sender_email: str) -> bool:
        """Return True if the Zoho INBOX has any mail FROM the given address."""
        conn = self._require_conn()
        try:
            status, _ = conn.select("INBOX", readonly=True)
            if status != "OK":
                return False
            status, data = conn.search(None, f'(FROM "{sender_email}")')
            if status != "OK":
                return False
            return bool(data and data[0] and data[0].split())
        except Exception as e:
            logger.warning("inbox_has_from error for %s: %s", sender_email, e)
            return False
