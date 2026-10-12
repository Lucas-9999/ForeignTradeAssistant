"""
Template Rotation + Anti-Spam Strategy.

Rotation: picks the least-recently-used active template in a category.
Anti-spam: enforces daily caps, tiered per-domain limits, MX validation,
           and randomized inter-send delays.

All checks happen BEFORE attempting to send. If a check fails, the send is rejected
with a descriptive reason — no email leaves the system without passing every guard.

Domain Tier System:
  Tier 1 (FREE_EMAIL)  — shared infrastructure (gmail, hotmail, yahoo, etc.)
                         Higher per-domain limit (default 25) since these providers
                         handle billions of messages and individual senders are
                         a tiny fraction of total volume.
  Tier 2 (BUSINESS)     — dedicated organization mail servers (everything else)
                         Lower per-domain limit (default 15) since each domain maps
                         to a specific company whose IT team may notice patterns.
"""

import enum
import json
import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Optional

import config
from services.email_reputation.models import SuppressionReason
from services.email_reputation.suppression_service import (
    SuppressionPersistenceError,
    SuppressionService,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Domain Tier Classification
# ═══════════════════════════════════════════════════════════════════════════


class DomainTier(enum.Enum):
    """Classification of email domains for rate-limiting purposes."""
    FREE_EMAIL = 1   # Shared infrastructure — higher limit
    BUSINESS = 2     # Dedicated organization server — lower limit


# Known free/public email provider domains.
# These run multi-tenant infrastructure where a single sender is
# a negligible fraction of total volume, so higher limits are safe.
FREE_EMAIL_DOMAINS: frozenset[str] = frozenset({
    # Google
    "gmail.com", "googlemail.com",
    # Microsoft
    "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de",
    "outlook.com", "outlook.fr", "outlook.de", "outlook.co.jp",
    "live.com", "live.fr", "live.co.uk", "live.de", "live.jp",
    "msn.com",
    # Yahoo
    "yahoo.com", "ymail.com", "rocketmail.com",
    "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.co.jp",
    "yahoo.com.br", "yahoo.com.au", "yahoo.in", "yahoo.ca",
    # Apple
    "icloud.com", "me.com", "mac.com",
    # AOL
    "aol.com", "aim.com",
    # Proton
    "proton.me", "protonmail.com", "pm.me",
    # Yandex
    "yandex.com", "yandex.ru", "ya.ru",
    # Mail.ru group
    "mail.ru", "inbox.ru", "list.ru", "bk.ru",
    # Chinese providers
    "qq.com", "foxmail.com",
    "163.com", "126.com", "yeah.net",
    "sina.com", "sina.cn",
    "sohu.com",
    # Other international
    "zoho.com", "zohomail.com",
    "fastmail.com", "fastmail.fm",
    "tutanota.com", "tutamail.com", "tuta.io",
    "gmx.com", "gmx.de", "gmx.net", "web.de",
    "mail.com", "email.com",
    "rediffmail.com",
    "onet.pl", "wp.pl", "o2.pl", "interia.pl",
    "seznam.cz", "post.cz",
    "libero.it",
    # Korean
    "hanmail.net", "daum.net", "nate.com",
    # Brazilian
    "uol.com.br", "bol.com.br", "terra.com.br", "ig.com.br",
    # Indian
    "rediffmail.com",
    # Japanese
    "ezweb.ne.jp", "i.softbank.jp", "docomo.ne.jp",
})


def classify_domain(email: str) -> tuple[DomainTier, int]:
    """Classify an email's domain and return (tier, per_domain_limit).

    Args:
        email: Full email address (e.g. 'user@gmail.com').

    Returns:
        (DomainTier, per_domain_limit) — the tier and its max sends/day.
    """
    domain = email.strip().lower().split("@")[-1] if "@" in email else "unknown"
    if domain in FREE_EMAIL_DOMAINS:
        return DomainTier.FREE_EMAIL, config.FREE_EMAIL_DOMAIN_LIMIT
    return DomainTier.BUSINESS, config.BUSINESS_DOMAIN_LIMIT


@dataclass(frozen=True)
class AntiSpamVerdict:
    """Structured pre-send verdict for one recipient (P4 SendingGuard input).

    Mirrors the daily-quota → MX → per-domain tier section of ``can_send`` with
    the same checks in the same order, but returns closed ``blocked_by`` codes
    instead of free-text reasons. Suppression is intentionally NOT repeated here
    — the SendingGuard owns suppression and evaluates it first. No side effects.
    """

    allowed: bool
    blocked_by: Optional[str] = None  # "daily_quota" | "no_mx" | "domain_quota"
    daily_remaining: int = 0
    daily_limit: int = 0
    domain_remaining: int = 0
    domain_limit: int = 0
    has_mx: bool = True


class AntiSpamGuard:
    """Rate limiter and anti-spam enforcer for outbound emails.

    Thread-safe: all state is in SQLite, no in-memory caches.

    Check sequence in can_send():
      1. Blacklist — permanently blocked addresses
      2. Daily total cap — absolute ceiling for the day
      3. MX record — domain must have a mail server (fail-open on DNS errors)
      4. Tiered per-domain limit — free email (25/day) vs business (15/day)
    """

    def __init__(self, conn: sqlite3.Connection, mailbox: str = "default"):
        """Rate limiter for one mailbox's sends.

        Args:
            conn: DB connection.
            mailbox: "default" → the NetEase batch quota (sending_quotas,
                DAILY_EMAIL_LIMIT). "zoho" → the personal Zoho mailbox quota
                (mailbox_quotas, ZOHO_DAILY_LIMIT) so the two never compete.
        """
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.mailbox = mailbox
        # Only the NetEase/default counter lives in sending_quotas; every other
        # mailbox (currently zoho) has its own row in mailbox_quotas.
        self._table = "mailbox_quotas" if mailbox != "default" else "sending_quotas"
        self.suppression_service = SuppressionService(conn)
        self._ensure_today_quota()
        # Per-mailbox daily cap resolved ONCE at construction (not per-email):
        # mailboxes.daily_limit (V8) wins, env limits fill in for pre-V8 DBs.
        self._limit = self._resolve_daily_limit()

    def _resolve_daily_limit(self) -> int:
        """Daily cap from the mailboxes table (V8), else env limits.

        V8-seeded rows store the same value the env limit would give, so this
        is a strict no-op for default/zoho until the admin edits a mailbox in
        the CRM console. Wrapped in try/except so pre-V8 test DBs (no
        mailboxes table) keep working with env limits.
        """
        try:
            row = self.conn.execute(
                "SELECT daily_limit FROM mailboxes WHERE name = ?", (self.mailbox,)
            ).fetchone()
            if row and row["daily_limit"]:
                return int(row["daily_limit"])
        except sqlite3.Error:
            pass  # pre-V8 DB — mailboxes table doesn't exist
        if self.mailbox == "zoho":
            return config.ZOHO_DAILY_LIMIT
        return config.DAILY_EMAIL_LIMIT

    def _daily_limit(self) -> int:
        return self._limit

    def _quota_where(self) -> tuple[str, tuple]:
        """(SQL WHERE clause, params) selecting this mailbox's quota row."""
        if self._table == "mailbox_quotas":
            return "date = ? AND mailbox = ?", (date.today().isoformat(), self.mailbox)
        return "date = ?", (date.today().isoformat(),)

    def _ensure_today_quota(self) -> None:
        """Create today's quota row if it doesn't exist."""
        today = date.today().isoformat()
        if self._table == "mailbox_quotas":
            self.conn.execute(
                "INSERT OR IGNORE INTO mailbox_quotas (date, mailbox, email_count, domain_counts) "
                "VALUES (?, ?, 0, '{}')",
                (today, self.mailbox),
            )
        else:
            self.conn.execute(
                "INSERT OR IGNORE INTO sending_quotas (date, email_count, domain_counts) VALUES (?, 0, '{}')",
                (today,),
            )
        self.conn.commit()

    # ── Pre-send checks ──────────────────────────────────────────────────

    def can_send(self, email: str) -> tuple[bool, str]:
        """Check if an email can be sent right now.

        Returns:
            (allowed: bool, reason: str) — reason is empty if allowed.
        """
        # 1. Unified suppression check (canonical table + legacy blacklist).
        suppression = self.suppression_service.check(email)
        if suppression.blocked:
            reason_code = (
                suppression.reason.value if suppression.reason else "unknown"
            )
            if suppression.is_legacy:
                return False, (
                    "Recipient is suppressed (legacy blacklisted) "
                    f"({reason_code})"
                )
            scope = suppression.scope.value if suppression.scope else "unknown"
            return False, (
                "Recipient is suppressed "
                f"({reason_code}; scope={scope})"
            )

        # 2-4. Daily cap → MX → per-domain tier (structured verdict; the
        # P4 SendingGuard consumes the same verdict via pre_send_verdict, so
        # the two paths can never disagree about quota/MX/domain state).
        verdict = self.pre_send_verdict(email)
        if not verdict.allowed:
            domain = self._extract_domain(email)
            if verdict.blocked_by == "daily_quota":
                return False, f"Daily limit reached ({verdict.daily_limit})"
            if verdict.blocked_by == "no_mx":
                return False, f"No MX record for domain {domain}"
            if verdict.blocked_by == "domain_quota":
                return False, (
                    f"Domain limit reached for {domain} ({verdict.domain_limit})"
                )
            return False, "Anti-spam limit reached"

        return True, ""

    def pre_send_verdict(
        self, email: str, *, ignore_daily_quota: bool = False
    ) -> AntiSpamVerdict:
        """Structured, read-only verdict for the P4 SendingGuard.

        Runs the same daily-quota → MX → per-domain tier checks as ``can_send``
        (same order, same queries) and returns closed codes. The suppression
        check is excluded — the SendingGuard evaluates it first, so composing
        this verdict after a suppression pass is exactly ``can_send``.

        ``ignore_daily_quota`` skips only the daily-cap short-circuit so the
        caller can still see MX / per-domain tier state when the daily cap is
        the current blocker (used by the SendingGuard's operator
        ``override_daily_quota`` path — the override must not silently skip
        MX / domain checks).

        Returns:
            AntiSpamVerdict — `allowed` is False and `blocked_by` is set on the
            first failing check, mirroring can_send's short-circuit order.
        """
        where, params = self._quota_where()
        row = self.conn.execute(
            f"SELECT email_count FROM {self._table} WHERE {where}", params
        ).fetchone()
        daily_count = row["email_count"] if row else 0
        limit = self._daily_limit()
        daily_remaining = max(0, limit - daily_count)

        if daily_count >= limit and not ignore_daily_quota:
            return AntiSpamVerdict(
                allowed=False,
                blocked_by="daily_quota",
                daily_remaining=0,
                daily_limit=limit,
            )

        has_mx = self._check_mx(email)
        if not has_mx:
            return AntiSpamVerdict(
                allowed=False,
                blocked_by="no_mx",
                daily_remaining=daily_remaining,
                daily_limit=limit,
                has_mx=False,
            )

        domain = self._extract_domain(email)
        _tier, tier_limit = classify_domain(email)
        domain_count = self._get_domain_count(domain)
        if domain_count >= tier_limit:
            return AntiSpamVerdict(
                allowed=False,
                blocked_by="domain_quota",
                daily_remaining=daily_remaining,
                daily_limit=limit,
                domain_remaining=0,
                domain_limit=tier_limit,
                has_mx=True,
            )

        return AntiSpamVerdict(
            allowed=True,
            daily_remaining=daily_remaining,
            daily_limit=limit,
            domain_remaining=tier_limit - domain_count,
            domain_limit=tier_limit,
            has_mx=True,
        )

    def can_send_batch(self, emails: list[str]) -> tuple[bool, str, list[str]]:
        """Check a batch of emails. Returns (all_allowed, reason, blocked_emails)."""
        blocked = []
        for email in emails:
            ok, reason = self.can_send(email)
            if not ok:
                blocked.append(f"{email}: {reason}")

        if blocked:
            return False, f"{len(blocked)}/{len(emails)} blocked", blocked
        return True, "", []

    # ── Post-send recording ──────────────────────────────────────────────

    def record_send(self, email: str) -> None:
        """Record a successful send — increments this mailbox's quotas."""
        domain = self._extract_domain(email)

        # Get current domain counts
        where, params = self._quota_where()
        row = self.conn.execute(
            f"SELECT domain_counts FROM {self._table} WHERE {where}", params
        ).fetchone()

        domain_counts = {}
        if row and row["domain_counts"]:
            try:
                domain_counts = json.loads(row["domain_counts"])
            except (json.JSONDecodeError, TypeError):
                domain_counts = {}

        domain_counts[domain] = domain_counts.get(domain, 0) + 1

        if self._table == "mailbox_quotas":
            self.conn.execute(
                """UPDATE mailbox_quotas
                   SET email_count = email_count + 1,
                       domain_counts = ?
                   WHERE date = ? AND mailbox = ?""",
                (json.dumps(domain_counts), date.today().isoformat(), self.mailbox),
            )
        else:
            self.conn.execute(
                """UPDATE sending_quotas
                   SET email_count = email_count + 1,
                       domain_counts = ?
                   WHERE date = ?""",
                (json.dumps(domain_counts), date.today().isoformat()),
            )
        self.conn.commit()

    def record_bounce(self, email: str) -> None:
        """Record an already-confirmed hard bounce in the canonical store."""
        self.suppression_service.suppress_email(
            email,
            reason=SuppressionReason.HARD_BOUNCE,
            source="confirmed_bounce",
        )
        logger.warning("Confirmed hard bounce suppressed")

    def record_smtp_failure(
        self,
        email: str,
        diagnostic: object,
        *,
        provider: Optional[str] = None,
    ) -> bool:
        """Persist only SMTP failures proven to be recipient hard bounces."""
        try:
            record = self.suppression_service.suppress_from_smtp_error(
                email,
                diagnostic,
                provider=provider or self.mailbox,
            )
        except SuppressionPersistenceError:
            self.conn.rollback()
            self._add_to_blacklist(email, "hard_bounce_fallback")
            logger.error(
                "Canonical hard-bounce persistence failed; legacy fail-closed "
                "fallback installed"
            )
            return True
        if record is None:
            return False
        logger.warning("Confirmed SMTP hard bounce suppressed")
        return True

    # ── Quota queries ────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get current anti-spam status including tier breakdown.

        Scoped to this guard's mailbox: "default" reports the NetEase batch
        quota, "zoho" reports the personal Zoho mailbox quota.
        """
        where, params = self._quota_where()
        row = self.conn.execute(
            f"SELECT email_count, domain_counts FROM {self._table} WHERE {where}", params
        ).fetchone()

        email_count = row["email_count"] if row else 0
        domain_counts = {}
        if row and row["domain_counts"]:
            try:
                domain_counts = json.loads(row["domain_counts"])
            except (json.JSONDecodeError, TypeError):
                pass

        limit = self._daily_limit()

        # Tier breakdown
        tier_sent = {tier.name: 0 for tier in DomainTier}
        tier_limits = {
            DomainTier.FREE_EMAIL.name: config.FREE_EMAIL_DOMAIN_LIMIT,
            DomainTier.BUSINESS.name: config.BUSINESS_DOMAIN_LIMIT,
        }
        for domain, count in domain_counts.items():
            test_email = f"check@{domain}"
            tier, _ = classify_domain(test_email)
            tier_sent[tier.name] += count

        return {
            "emails_sent_today": email_count,
            "daily_limit": limit,
            "remaining": max(0, limit - email_count),
            "mailbox": self.mailbox,
            "per_domain_limit": {
                "free_email": config.FREE_EMAIL_DOMAIN_LIMIT,
                "business": config.BUSINESS_DOMAIN_LIMIT,
            },
            "tier_sent": tier_sent,
            "tier_limits": tier_limits,
            "domain_counts": domain_counts,
            "next_reset": f"{date.today().isoformat()}T23:59:59",
        }

    # ── Helpers ──────────────────────────────────────────────────────────

    def _is_blacklisted(self, email: str) -> bool:
        """Compatibility helper for callers that still use the legacy name."""
        return self.suppression_service.check(email).blocked

    def _add_to_blacklist(self, email: str, reason: str) -> None:
        try:
            self.conn.execute(
                "INSERT INTO blacklist (email, reason) VALUES (?, ?)",
                (email.strip().lower(), reason),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def _get_domain_count(self, domain: str) -> int:
        where, params = self._quota_where()
        row = self.conn.execute(
            f"SELECT domain_counts FROM {self._table} WHERE {where}", params
        ).fetchone()
        if not row or not row["domain_counts"]:
            return 0
        try:
            counts = json.loads(row["domain_counts"])
            return counts.get(domain, 0)
        except (json.JSONDecodeError, TypeError):
            return 0

    def _check_mx(self, email: str) -> bool:
        """Check whether the email's domain has MX records.

        Results are cached per domain for 1 hour. Returns True (fail-open)
        on DNS errors to avoid blocking valid leads during transient outages.
        """
        domain = self._extract_domain(email)
        try:
            from utils.email_verify import has_mx_record
            return has_mx_record(domain)
        except Exception:
            return True  # fail-open: don't block on transient DNS errors

    @staticmethod
    def _extract_domain(email: str) -> str:
        """Extract domain from email, e.g. 'user@gmail.com' → 'gmail.com'."""
        parts = email.strip().lower().split("@")
        return parts[1] if len(parts) == 2 else "unknown"


def compute_send_delay() -> float:
    """Compute a randomized delay between sends (in seconds).

    Returns a random float between MIN_SEND_INTERVAL_SEC and MAX_SEND_INTERVAL_SEC.
    """
    return random.uniform(config.MIN_SEND_INTERVAL_SEC, config.MAX_SEND_INTERVAL_SEC)
