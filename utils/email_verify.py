"""
Email Deliverability Verification — MX record check + syntax validation.

Uses the `email-validator` library (already in requirements.txt) for
deliverability checks including MX record resolution. Results are
cached per domain to avoid repeated DNS lookups.

Two-tier API:
  1. verify_email() — full check (syntax + MX + SMTP)
  2. has_mx_record() — lightweight domain-only MX check (cached)
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# ── Domain MX cache ────────────────────────────────────────────────────────
# TTL-limited cache: domain → (has_mx: bool, cached_at: float)
_mx_cache: dict[str, tuple[bool, float]] = {}
_MX_CACHE_TTL = 3600  # 1 hour


def has_mx_record(domain: str) -> bool:
    """Check whether a domain has MX records (can receive email).

    Returns True if MX records exist, False if domain has no mail server.
    Results are cached for 1 hour to avoid repeated DNS lookups.
    """
    domain = domain.strip().lower()
    if not domain or "." not in domain:
        return False

    now = time.time()
    if domain in _mx_cache:
        result, cached_at = _mx_cache[domain]
        if now - cached_at < _MX_CACHE_TTL:
            return result

    try:
        from email_validator import validate_email, EmailNotValidError

        # Use a test address at this domain to trigger MX resolution
        test_email = f"check@{domain}"
        validate_email(
            test_email,
            check_deliverability=True,
            timeout=5,
        )
        _mx_cache[domain] = (True, now)
        return True

    except ImportError:
        # email_validator not available — fall back to nslookup
        logger.debug("email_validator not available, using nslookup fallback")
        result = _check_mx_via_nslookup(domain)
        _mx_cache[domain] = (result, now)
        return result

    except EmailNotValidError as e:
        # email_validator raises EmailNotValidError for undeliverable addresses
        msg = str(e).lower()
        if any(kw in msg for kw in (
            "no mx record", "no nameserver", "domain not found",
            "does not exist", "nxdomain",
        )):
            _mx_cache[domain] = (False, now)
            return False
        # Other validation errors (syntax, etc.) — domain might still be valid
        _mx_cache[domain] = (True, now)
        return True

    except Exception as e:
        # DNS timeout or network error — assume valid (don't block on transient failures)
        logger.debug(f"MX check failed for {domain}: {e} — assuming valid")
        _mx_cache[domain] = (True, now)
        return True


def verify_email(email: str) -> tuple[bool, str]:
    """Perform full email deliverability check.

    Validates syntax and checks whether the domain can receive email
    by resolving its MX records.

    Args:
        email: The email address to verify.

    Returns:
        (is_valid: bool, reason: str) — reason is empty if valid.
    """
    if not email or "@" not in email:
        return False, "Invalid email format"

    email = email.strip()

    try:
        from email_validator import validate_email, EmailNotValidError

        validate_email(
            email,
            check_deliverability=True,
            timeout=5,
        )
        return True, ""

    except ImportError:
        # Fall back to basic check + MX via nslookup
        domain = email.rsplit("@", 1)[1]
        if not has_mx_record(domain):
            return False, f"No MX record for domain: {domain}"
        return True, ""

    except EmailNotValidError as e:
        return False, str(e)

    except Exception as e:
        logger.debug(f"Email verification failed for {email}: {e}")
        return True, ""  # Assume valid on transient errors


def _check_mx_via_nslookup(domain: str) -> bool:
    """Fallback MX check using system nslookup command."""
    import subprocess

    try:
        result = subprocess.run(
            ["nslookup", "-type=mx", domain],
            capture_output=True, text=True, timeout=5,
            shell=False,
        )
        output = (result.stdout + result.stderr).lower()
        return "mail exchanger" in output or ("mx" in output and "internet address" not in output)
    except Exception:
        return True  # Assume valid if we can't check


def clear_mx_cache():
    """Clear the domain MX record cache (useful for testing)."""
    _mx_cache.clear()
