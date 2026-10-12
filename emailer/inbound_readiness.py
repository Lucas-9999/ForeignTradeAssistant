"""Fail-closed IMAP readiness check for P13 normal outbound delivery."""

from __future__ import annotations

import logging
import threading
import time

import config
from collector.scheduler import scheduler_is_running
from emailer.inbox import create_imap_connection

logger = logging.getLogger(__name__)

_CHECK_TTL_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 30.0
_lock = threading.Lock()
_cached_results: dict[str, tuple[bool, str, float]] = {}


def normal_outbound_delivery_ready(smtp_profile: str) -> tuple[bool, str]:
    """Verify the configured DSN monitor can authenticate before SMTP.

    A successful check is cached briefly for a batch; a failed check is cached
    only long enough to avoid a connection storm.  No mailbox content is read.
    """
    settings = _imap_settings(smtp_profile)
    if settings is None:
        return False, f"{smtp_profile} IMAP monitor is not configured"
    host, port, username, password = settings
    if not username or not password:
        return False, f"{smtp_profile} IMAP monitor credentials are unavailable"
    if not scheduler_is_running():
        return False, f"{smtp_profile} IMAP monitor scheduler is not running"

    now = time.monotonic()
    with _lock:
        cached = _cached_results.get(smtp_profile)
        if cached is not None and now < cached[2]:
            return cached[0], cached[1]
        try:
            connection = create_imap_connection(
                host, port, username, password,
            )
            status, _ = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("INBOX selection was not accepted")
            try:
                connection.logout()
            except Exception:
                # Authentication was verified.  A logout response failure must
                # not be mistaken for an unavailable inbound monitor.
                logger.warning("P13 IMAP readiness logout did not complete")
            result = (
                True,
                f"{smtp_profile} IMAP monitor ready",
                now + _CHECK_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning("P13 IMAP readiness failed: %s", type(exc).__name__)
            result = (
                False,
                f"{smtp_profile} IMAP monitor connection failed",
                now + _FAILURE_TTL_SECONDS,
            )
        _cached_results[smtp_profile] = result
        return result[0], result[1]


def _imap_settings(smtp_profile: str) -> tuple[str, int, str, str] | None:
    if smtp_profile == "default":
        return (
            config.IMAP_HOST,
            config.IMAP_PORT,
            config.IMAP_USERNAME,
            config.IMAP_PASSWORD,
        )
    if smtp_profile == "zoho":
        return (
            config.ZOHO_IMAP_HOST,
            config.ZOHO_IMAP_PORT,
            config.ZOHO_IMAP_USERNAME,
            config.ZOHO_IMAP_PASSWORD,
        )
    return None
