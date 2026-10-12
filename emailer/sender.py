"""
Email Sender — async SMTP delivery with sync fallback.

Uses aiosmtplib for async sending (non-blocking in FastAPI context).
Falls back to stdlib smtplib if aiosmtplib is not available.
"""

import logging
import smtplib
import ssl
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from typing import Optional

import config
from crm import mailbox_manager
from services.email_reputation.diagnostic_safety import sanitize_diagnostic

logger = logging.getLogger(__name__)

# Try async SMTP first
try:
    import aiosmtplib

    HAS_ASYNC_SMTP = True
except ImportError:
    HAS_ASYNC_SMTP = False
    logger.warning("aiosmtplib not installed — using sync smtplib fallback")


def _smtp_settings(profile: str = "default") -> dict:
    """Return {host, port, use_tls, username, password, from_email, from_name}.

    M5 (V8): DB-first resolver with env fallback — a `mailboxes` row for this
    profile wins, env fills gaps. `default`/`zoho` are env-seeded at migration
    with password_enc=NULL, so resolution returns byte-identical credentials to
    the pre-V8 behavior unless the admin edited the mailbox in the CRM console
    (the whole point of M5). Read at call time so a single process can switch
    profiles between sends (e.g. batch 孚盟 data via Zoho while the scheduler
    keeps using NetEase).
    """
    return mailbox_manager.resolve_smtp_settings(profile)


async def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    to_name: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> tuple[bool, str, Optional[str]]:
    """Send an HTML email.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        html_body: Rendered HTML body.
        to_name: Optional recipient display name.
        from_email: Override sender email (defaults to config).
        from_name: Override sender name (defaults to config).
        reply_to: Optional Reply-To address.

    Returns:
        Tuple of (success: bool, message: str, message_id: Optional[str]).
        message_id is the RFC 2822 Message-ID for reply tracking (IMAP Phase 2).
    """
    # ── Validate config + build MIME (shared with the sync path) ──────
    msg, username, password, err = _prepare_email(
        to_email, subject, html_body, to_name, _smtp_settings("default"),
        from_email, from_name, reply_to,
    )
    if err:
        return False, err, None

    # ── Send ──────────────────────────────────────────────────────────
    settings = _smtp_settings("default")
    if HAS_ASYNC_SMTP:
        success, message = await _send_async(
            msg, to_email, username, password,
            settings["host"], settings["port"], settings["use_tls"],
        )
    else:
        success, message = _send_sync(
            msg, to_email, username, password,
            settings["host"], settings["port"], settings["use_tls"],
        )
    return success, message, msg["Message-ID"] if success else None


def _prepare_email(
    to_email: str,
    subject: str,
    html_body: str,
    to_name: Optional[str] = None,
    settings: Optional[dict] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> tuple[Optional[MIMEMultipart], Optional[str], Optional[str], Optional[str]]:
    """Validate config and build the MIME message (shared by async+sync paths).

    Returns (msg, username, password, error). If error is not None the other
    fields are None and the caller should short-circuit with that error.
    """
    settings = settings or _smtp_settings("default")
    username = from_email or settings["username"]
    password = settings["password"]

    if not username or not password:
        return None, None, None, "SMTP credentials not configured (FTA_SMTP_USERNAME / FTA_SMTP_PASSWORD)"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(
        (from_name or settings["from_name"], from_email or settings["from_email"])
    )
    msg["To"] = formataddr((to_name or "", to_email))
    if reply_to:
        msg["Reply-To"] = reply_to

    # Generate a unique Message-ID for reply tracking
    msg["Message-ID"] = make_msgid(domain="momafastener.com")

    # Plain-text fallback: strip HTML tags but preserve structure
    import re
    plain = re.sub(r"<br\s*/?>", "\n", html_body)
    plain = re.sub(r"</p>\s*<p>", "\n\n", plain)
    plain = re.sub(r"</?p>", "", plain)
    plain = re.sub(r"<[^>]+>", "", plain)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    plain = plain.strip()

    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    return msg, username, password, None


async def _send_async(
    msg: MIMEMultipart,
    to_email: str,
    username: str,
    password: str,
    host: str,
    port: int,
    use_tls: bool,
) -> tuple[bool, str]:
    """Send via aiosmtplib (async).

    Config mapping (aiosmtplib 5.x semantics):
      - SMTP_USE_TLS=true  → start_tls=True,  use_tls=False  (port 587, STARTTLS)
      - SMTP_USE_TLS=false → start_tls=False, use_tls=True   (port 465, direct SSL)
    """
    try:
        # SMTP_USE_TLS=false → direct SSL (use_tls=True)
        # SMTP_USE_TLS=true  → STARTTLS   (start_tls=True)
        use_direct_ssl = not use_tls
        tls_context = ssl.create_default_context()

        await aiosmtplib.send(
            msg,
            hostname=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_direct_ssl,
            start_tls=use_tls,
            tls_context=tls_context,
            timeout=30,
        )
        logger.info(f"Email sent (async): {to_email}")
        return True, "sent"
    except aiosmtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed — check username/password"
    except aiosmtplib.SMTPConnectError:
        return False, f"Could not connect to {host}:{port}"
    except aiosmtplib.SMTPRecipientsRefused as e:
        return False, sanitize_diagnostic(f"Recipient refused: {e}", max_length=None)
    except aiosmtplib.SMTPException as e:
        msg_str = sanitize_diagnostic(e, max_length=None)
        # Detect hard bounces by SMTP 5xx permanent failure codes
        if _is_hard_bounce(msg_str):
            logger.warning("Potential hard bounce detected: %s", msg_str[:120])
            return False, f"HARD_BOUNCE: {msg_str}"
        return False, f"SMTP error: {msg_str}"
    except RuntimeError as e:
        safe_error = sanitize_diagnostic(e, max_length=None)
        if "interpreter shutdown" in str(e) or "schedule new futures" in str(e):
            logger.warning("Event loop shutting down — skipping SMTP send")
            return False, f"Shutdown: {safe_error}"
        logger.error("Runtime error during SMTP send: %s", safe_error)
        return False, f"Runtime error: {safe_error}"
    except Exception as e:
        safe_error = sanitize_diagnostic(e, max_length=None)
        logger.error("Unexpected SMTP error: %s", safe_error)
        return False, f"Unexpected error: {safe_error}"


def send_email_sync(
    to_email: str,
    subject: str,
    html_body: str,
    to_name: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    reply_to: Optional[str] = None,
    smtp_profile: str = "default",
) -> tuple[bool, str, Optional[str]]:
    """Fully synchronous send via stdlib smtplib — NO asyncio.

    Safe to call from background threads (APScheduler jobs, follow-up engine,
    email batch) even while other threads run asyncio crawls. The async path
    (`send_email`) repeatedly hit "cannot schedule new futures after shutdown"
    on Windows when multiple ProactorEventLoops ran concurrently in one
    process (observed 2026-08-05: batch died at email #29 while two xunkexing
    crawls were running). SMTP sending is inherently sequential — a fresh
    smtplib connection per message, no event loop involved.
    """
    settings = _smtp_settings(smtp_profile)
    msg, username, password, err = _prepare_email(
        to_email, subject, html_body, to_name, settings, from_email, from_name, reply_to,
    )
    if err:
        return False, err, None

    success, message = _send_sync(
        msg, to_email, username, password,
        settings["host"], settings["port"], settings["use_tls"],
    )
    return success, message, msg["Message-ID"] if success else None


def _send_sync(
    msg: MIMEMultipart,
    to_email: str,
    username: str,
    password: str,
    host: str,
    port: int,
    use_tls: bool,
) -> tuple[bool, str]:
    """Send via stdlib smtplib (sync fallback)."""
    try:
        context = ssl.create_default_context() if use_tls else None

        if use_tls:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls(context=context)
        else:
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)

        server.login(username, password)
        server.send_message(msg)
        server.quit()

        logger.info(f"Email sent (sync): {to_email}")
        return True, "sent"
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed — check username/password"
    except smtplib.SMTPConnectError:
        return False, f"Could not connect to {host}:{port}"
    except smtplib.SMTPRecipientsRefused as e:
        return False, sanitize_diagnostic(f"Recipient refused: {e}", max_length=None)
    except smtplib.SMTPException as e:
        msg_str = sanitize_diagnostic(e, max_length=None)
        if _is_hard_bounce(msg_str):
            logger.warning("Potential hard bounce detected: %s", msg_str[:120])
            return False, f"HARD_BOUNCE: {msg_str}"
        return False, f"SMTP error: {msg_str}"
    except Exception as e:
        safe_error = sanitize_diagnostic(e, max_length=None)
        logger.error("Unexpected SMTP error: %s", safe_error)
        return False, f"Unexpected error: {safe_error}"


def _is_hard_bounce(error_msg: str) -> bool:
    """Detect SMTP hard bounces (5xx permanent failures) from error text.

    SMTP 5xx codes indicate permanent delivery failure:
      550 — mailbox unavailable / user unknown
      551 — user not local
      552 — exceeded storage allocation (mailbox full)
      553 — mailbox name not allowed
      554 — transaction failed (often spam-related)
      5.1.1 — bad destination mailbox address
      5.1.2 — bad destination system address
      5.2.2 — mailbox full
    """
    import re
    msg = error_msg.upper()
    # Match SMTP status codes: 5xx or 5.x.x
    if re.search(r"\b5[0-9][0-9]\b", msg):
        return True
    if re.search(r"\b5\.[0-9]\.[0-9]\b", msg):
        return True
    # Common bounce keywords in replies
    bounce_keywords = [
        "MAILBOX NOT FOUND", "USER UNKNOWN", "NO SUCH USER",
        "INVALID RECIPIENT", "ADDRESS REJECTED", "RECIPIENT REJECTED",
        "MAILBOX UNAVAILABLE", "DOES NOT EXIST", "INVALID MAILBOX",
        "UNDELIVERABLE", "NOT DELIVERED",
    ]
    for kw in bounce_keywords:
        if kw in msg:
            return True
    return False
