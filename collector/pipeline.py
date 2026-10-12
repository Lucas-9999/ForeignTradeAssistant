"""
Collection Pipeline — cleaning, mapping, dedup, and CRM ingestion.

All sources feed raw dicts into this pipeline, which normalizes them into
Lead dataclasses and feeds them into LeadManager.create_batch().

Pattern: mirrors PriceGod's pipeline approach — stateless functions that
transform data through discrete stages.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import config
from crm.manager import LeadManager
from crm.models import Lead
from utils.sanitize import sanitize

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

# Regex for basic email validation
EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# ── Disposable email domains (burner / throwaway addresses) ────────────
# These are never legitimate business contacts.
DISPOSABLE_DOMAINS: set[str] = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "temp-mail.org",
    "tempmail.com", "throwaway.email", "yopmail.com", "sharklasers.com",
    "trashmail.com", "dispostable.com", "fakeinbox.com", "getnada.com",
    "maildrop.cc", "harakirimail.com", "spam4.me", "spambog.com",
    "temporary-mail.net", "wegwerfmail.de", "dayrep.com", "einrot.com",
    "fleckens.hu", "gustr.com", "jourrapide.com", "teleworm.us",
    "superrito.com", "armyspy.com", "cuvox.de", "dayrep.com",
    "einrot.com", "fleckens.hu", "gustr.com", "jourrapide.com",
    "rhyta.com", "teleworm.us", "trash-mail.de", "maileater.com",
    "33mail.com", "anonaddy.com", "simplelogin.com", "moakt.com",
    "emailondeck.com", "mytemp.email", "guerrillamailblock.com",
    "guerrillamail.de", "sharklazers.com", "spambox.us", "spamgourmet.com",
    "jetable.org", "no-spam.ws", "mailnull.com",
}

# ── Role-based email prefixes (generic/group addresses) ────────────────
# These rarely reach an individual decision-maker and often bounce.
ROLE_EMAIL_PREFIXES: set[str] = {
    # These rarely reach an individual and often bounce or auto-reply.
    "noreply", "no-reply", "nobody", "root", "test",
    "postmaster", "hostmaster", "webmaster", "abuse",
    "mail", "hello", "help",
    # Note: 'sales', 'info', 'contact', 'office' are intentionally NOT blocked —
    # they are common legitimate B2B addresses at small/medium importers.
}

# ── Common domain typo correction ──────────────────────────────────────
# Auto-fix before sending. Only correct obvious typos (single char change).
DOMAIN_TYPO_MAP: dict[str, str] = {
    "gmial.com": "gmail.com",
    "gmail.cm": "gmail.com",
    "gmal.com": "gmail.com",
    "hotmal.com": "hotmail.com",
    "hotmial.com": "hotmail.com",
    "hotmai.com": "hotmail.com",
    "yaho.com": "yahoo.com",
    "yahooo.com": "yahoo.com",
    "yahhoo.com": "yahoo.com",
    "outlok.com": "outlook.com",
    "outloo.com": "outlook.com",
    "outlook.cm": "outlook.com",
    "protonmai.com": "protonmail.com",
    "aol.cm": "aol.com",
    "icloud.cm": "icloud.com",
    "yandex.cm": "yandex.com",
    "mail.comn": "mail.com",
}

# Known junk/non-business emails to skip
JUNK_EMAIL_PATTERNS = [
    re.compile(r"@example\.com$", re.I),
    re.compile(r"@test\.com$", re.I),
    re.compile(r"^test@", re.I),
    re.compile(r"^admin@", re.I),
    re.compile(r"^noreply@", re.I),
    re.compile(r"^no-?reply@", re.I),
]

# TLD → country inference (common mappings for fastener export markets)
TLD_COUNTRY_MAP: dict[str, str] = {
    "de": "Germany",      "at": "Austria",       "ch": "Switzerland",
    "fr": "France",       "be": "Belgium",        "nl": "Netherlands",
    "it": "Italy",        "es": "Spain",          "pt": "Portugal",
    "pl": "Poland",       "cz": "Czech Republic", "sk": "Slovakia",
    "uk": "UK",           "ie": "Ireland",
    "se": "Sweden",       "no": "Norway",         "dk": "Denmark",
    "fi": "Finland",      "ee": "Estonia",        "lv": "Latvia",
    "lt": "Lithuania",
    "us": "USA",          "ca": "Canada",         "mx": "Mexico",
    "br": "Brazil",       "ar": "Argentina",      "cl": "Chile",
    "jp": "Japan",        "kr": "South Korea",    "tw": "Taiwan",
    "in": "India",        "pk": "Pakistan",       "bd": "Bangladesh",
    "au": "Australia",    "nz": "New Zealand",
    "za": "South Africa", "ae": "UAE",            "sa": "Saudi Arabia",
    "tr": "Turkey",       "ru": "Russia",         "vn": "Vietnam",
    "th": "Thailand",     "my": "Malaysia",       "id": "Indonesia",
    "ph": "Philippines",  "sg": "Singapore",
    "com": None, "org": None, "net": None, "io": None, "co": None,  # ambiguous
}


# ═══════════════════════════════════════════════════════════════════════════
# Cleaning & Normalization
# ═══════════════════════════════════════════════════════════════════════════


def clean_text(value: Optional[str]) -> Optional[str]:
    """Strip whitespace and normalize empty strings to None."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def is_valid_email(email: Optional[str]) -> bool:
    """Check if an email looks valid and is not junk.

    Checks: syntax → junk patterns → disposable domain → role account.
    Returns False for anything that should be skipped entirely.
    """
    if not email:
        return False
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return False
    for pattern in JUNK_EMAIL_PATTERNS:
        if pattern.search(email):
            return False
    if _is_disposable_email(email):
        return False
    if _is_role_email(email):
        return False
    return True


def correct_email_typo(email: str) -> str:
    """Fix common domain typos in an email address.

    e.g. 'user@gmial.com' → 'user@gmail.com'
    """
    email = email.strip().lower()
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    if domain in DOMAIN_TYPO_MAP:
        corrected = f"{local}@{DOMAIN_TYPO_MAP[domain]}"
        logger.info(f"Email typo corrected: {email} → {corrected}")
        return corrected
    return email


def _is_disposable_email(email: str) -> bool:
    """Check if the email uses a disposable/temporary domain."""
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1]
    # Exact match
    if domain in DISPOSABLE_DOMAINS:
        return True
    # Also check parent domain (e.g. mail.temp-mail.org → temp-mail.org matches)
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in DISPOSABLE_DOMAINS:
            return True
    return False


def _is_role_email(email: str) -> bool:
    """Check if the email uses a generic role-based prefix."""
    if "@" not in email:
        return False
    local = email.rsplit("@", 1)[0]
    # Strip common suffixes like '+alias' or '-dept'
    local_base = local.split("+")[0].split("-")[0].split(".")[0]
    return local_base in ROLE_EMAIL_PREFIXES


def infer_country_from_tld(website: Optional[str]) -> Optional[str]:
    """Infer country from a website's TLD."""
    if not website:
        return None
    # Strip protocol and path
    domain = re.sub(r"^https?://", "", website)
    domain = domain.split("/")[0].split("?")[0]
    # Get TLD (last segment)
    parts = domain.rsplit(".", 1)
    if len(parts) >= 2:
        tld = parts[1].lower()
        return TLD_COUNTRY_MAP.get(tld)
    return None


def infer_country_from_phone(phone: Optional[str]) -> Optional[str]:
    """Infer country from phone prefix — covers major fastener markets."""
    if not phone:
        return None
    phone = phone.strip().lstrip("+")
    prefixes = {
        "49": "Germany",  "43": "Austria",     "41": "Switzerland",
        "33": "France",   "32": "Belgium",      "31": "Netherlands",
        "39": "Italy",    "34": "Spain",        "351": "Portugal",
        "48": "Poland",   "420": "Czech Republic",
        "44": "UK",       "353": "Ireland",
        "46": "Sweden",   "47": "Norway",       "45": "Denmark",
        "358": "Finland", "372": "Estonia",
        "1": "USA/Canada",
        "55": "Brazil",
        "81": "Japan",   "82": "South Korea",  "886": "Taiwan",
        "91": "India",
        "61": "Australia","64": "New Zealand",
        "90": "Turkey",
        "86": "China",
    }
    for code, country in sorted(prefixes.items(), key=lambda x: -len(x[0])):
        if phone.startswith(code):
            return country
    return None


def normalize_website(url: Optional[str]) -> Optional[str]:
    """Normalize a URL: ensure scheme, strip trailing slash, lowercase host."""
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # Remove trailing slash for consistency
    if url.count("/") > 2:
        url = url.rstrip("/")
    return url


# ═══════════════════════════════════════════════════════════════════════════
# Lead Mapping
# ═══════════════════════════════════════════════════════════════════════════


def raw_to_lead(raw: dict, source: str) -> Optional[Lead]:
    """Convert a raw lead dict into a normalized Lead dataclass.

    Returns None if the lead is too incomplete to be useful
    (no company name AND no email — nothing to work with).
    """
    company_name = clean_text(raw.get("company_name"))
    email = clean_text(raw.get("email"))
    contact_name = clean_text(raw.get("contact_name"))
    phone = clean_text(raw.get("phone"))
    whatsapp = clean_text(raw.get("whatsapp"))
    website = normalize_website(raw.get("website"))
    industry = clean_text(raw.get("industry"))
    country = clean_text(raw.get("country"))

    # ── Skip junk emails ──────────────────────────────────────────────
    if email:
        email = correct_email_typo(email)
    if email and not is_valid_email(email):
        logger.debug(f"Skipping lead with invalid/junk email: {email}")
        email = None  # still keep the lead, just without email

    # ── Infer missing fields ──────────────────────────────────────────
    # Try TLD → country
    if not country and website:
        country = infer_country_from_tld(website)
    # Try phone prefix → country
    if not country and phone:
        country = infer_country_from_phone(phone)

    # Sanitize industry: translate Chinese → English
    if industry:
        industry = sanitize(industry, fallback="fasteners")
    else:
        industry = "fasteners"

    # ── Minimum data check ────────────────────────────────────────────
    if not company_name and not email:
        logger.debug("Skipping lead: no company_name and no email")
        return None

    # ── Build source_detail ───────────────────────────────────────────
    source_detail = raw.get("source_detail")
    if isinstance(source_detail, dict):
        source_detail = json.dumps(source_detail, ensure_ascii=False)

    return Lead(
        company_name=company_name,
        contact_name=contact_name,
        email=email,
        phone=phone,
        whatsapp=whatsapp,
        website=website,
        source=source,
        source_url=raw.get("source_url"),
        source_detail=source_detail,
        industry=industry,
        country=country,
        notes=raw.get("notes"),
        status="new",
        discovered_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Ingestion
# ═══════════════════════════════════════════════════════════════════════════


def ingest_leads(
    raw_leads: list[dict],
    source: str,
    lead_mgr: LeadManager,
    dry_run: bool = False,
) -> dict:
    """Clean, map, dedup, and ingest a batch of raw leads into the CRM.

    Args:
        raw_leads: Raw dicts from a collector's collect() method.
        source: The source name (e.g. "xunkexing", "facebook").
        lead_mgr: An initialized LeadManager with an active connection.
        dry_run: If True, only clean & validate — don't write to DB.

    Returns:
        {
            "total_raw": int,         # raw dicts received
            "valid": int,             # passed cleaning (would be created)
            "skipped_invalid": int,   # failed cleaning (no name, junk email, etc.)
            "created": int,           # actually inserted (0 in dry_run)
            "duplicates_skipped": int,# duplicates detected by LeadManager
            "dry_run": bool,
        }
    """
    # ── Stage 1: Clean & Map ──────────────────────────────────────────
    leads: list[Lead] = []
    skipped = 0
    seen_emails: set[str] = set()

    for raw in raw_leads:
        lead = raw_to_lead(raw, source)
        if lead is None:
            skipped += 1
            continue

        # 2026-08-08 policy: no-email leads are useless for email outreach and
        # were bloating the DB (57k+ rows). Skip them at the ingestion boundary.
        if config.COLLECTION_REQUIRE_EMAIL and not lead.email:
            logger.debug(
                f"[{source}] Skipping lead without email: {lead.company_name}"
            )
            skipped += 1
            continue

        # In-batch dedup by email (avoid sending duplicates to LeadManager)
        # TODO(future): dedup is layered 3× — here, LeadManager.create_batch,
        # and create()/_find_duplicate. Consolidate into one path.
        if lead.email:
            email_key = lead.email.strip().lower()
            if email_key in seen_emails:
                logger.debug(f"In-batch duplicate: {lead.email}")
                skipped += 1
                continue
            seen_emails.add(email_key)

        leads.append(lead)

    if not leads:
        logger.warning(f"[{source}] No valid leads after cleaning (raw={len(raw_leads)}, skipped={skipped})")
        return {
            "total_raw": len(raw_leads),
            "valid": 0,
            "skipped_invalid": skipped,
            "created": 0,
            "duplicates_skipped": 0,
            "dry_run": dry_run,
        }

    # ── Stage 2: Ingest (or dry-run) ──────────────────────────────────
    if dry_run:
        logger.info(
            f"[{source}] DRY RUN — would create {len(leads)} leads "
            f"(raw={len(raw_leads)}, skipped={skipped})"
        )
        # Print a few samples
        for lead in leads[:5]:
            logger.info(f"  → {lead.company_name} | {lead.email} | {lead.country}")
        if len(leads) > 5:
            logger.info(f"  ... and {len(leads) - 5} more")
        return {
            "total_raw": len(raw_leads),
            "valid": len(leads),
            "skipped_invalid": skipped,
            "created": 0,
            "duplicates_skipped": 0,
            "dry_run": True,
        }

    created, dup_skipped = lead_mgr.create_batch(leads)
    logger.info(
        f"[{source}] Ingestion complete: "
        f"raw={len(raw_leads)}, valid={len(leads)}, "
        f"created={created}, db_duplicates={dup_skipped}, "
        f"invalid={skipped}"
    )
    return {
        "total_raw": len(raw_leads),
        "valid": len(leads),
        "skipped_invalid": skipped,
        "created": created,
        "duplicates_skipped": dup_skipped,
        "dry_run": False,
    }
