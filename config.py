"""
FTA Configuration — reads from .env for secrets, uses sensible defaults for paths.

Pattern: Path-based config for non-secrets (matching PriceGod), .env for credentials.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from emailer.transport import self_hosted_transport_enabled

if os.getenv("FTA_DEMO_MODE", "false").lower() != "true":
    load_dotenv()

# ── Paths ───────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("FTA_DB_PATH", BASE_DIR / "data" / "fta.db"))
DATA_DIR = Path(os.getenv("FTA_DATA_DIR", BASE_DIR / "data"))
TEMPLATES_DIR = Path(os.getenv("FTA_TEMPLATES_DIR", BASE_DIR / "templates" / "email"))
LOG_DIR = Path(os.getenv("FTA_LOG_DIR", BASE_DIR / "logs"))

# ── Database ────────────────────────────────────────────────────────────

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── SMTP / Email ────────────────────────────────────────────────────────

SMTP_HOST = os.getenv("FTA_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("FTA_SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("FTA_SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("FTA_SMTP_PASSWORD", "")
SMTP_USE_TLS = os.getenv("FTA_SMTP_USE_TLS", "true").lower() == "true"
SMTP_FROM_EMAIL = os.getenv("FTA_SMTP_FROM_EMAIL", SMTP_USERNAME)
SMTP_FROM_NAME = os.getenv("FTA_SMTP_FROM_NAME", "Foreign Trade Team")

# P9 self-hosted SMTP readiness gate.  It is deliberately OFF by default and
# does not select a transport yet: no production traffic can migrate merely by
# setting this flag.  A later separately approved phase must supply a transport.
SELF_HOSTED_SMTP_ENABLED = self_hosted_transport_enabled()

# Fictional sender identity for this portfolio snapshot.
SENDER_NAME = os.getenv("FTA_SENDER_NAME", "Example Team")

# ── Sender signature (used in plain-text email templates) ──────────────────
SENDER_WHATSAPP = os.getenv("FTA_SENDER_WHATSAPP", "")
SENDER_PHONE = os.getenv("FTA_SENDER_PHONE", "")
SENDER_EMAIL = os.getenv("FTA_SENDER_EMAIL", "sender@portfolio.invalid")
SENDER_ADDRESS = os.getenv("FTA_SENDER_ADDRESS", "Synthetic demo address")
SENDER_CERTS = os.getenv("FTA_SENDER_CERTS", "Synthetic template only")
SENDER_COMPANY = os.getenv("FTA_SENDER_COMPANY", "Example Operations")
SENDER_WEBSITE = os.getenv("FTA_SENDER_WEBSITE", "portfolio.invalid")

# ── IMAP (inbox monitoring — Phase 2) ───────────────────────────────────

IMAP_HOST = os.getenv("FTA_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("FTA_IMAP_PORT", "993"))
IMAP_USERNAME = os.getenv("FTA_IMAP_USERNAME", SMTP_USERNAME)
IMAP_PASSWORD = os.getenv("FTA_IMAP_PASSWORD", SMTP_PASSWORD)

# ── Zoho (personal mailbox — sent-follow-up tracker) ─────────────────────
# Zoho is used ONLY for manual personal follow-ups (operator@example.com).
# Bulk/auto sends stay on NetEase (FTA_SMTP_*/FTA_IMAP_*). No bulk sending
# through these credentials. IMAP requires an app-specific password when 2FA
# is enabled (Zoho Mail → Settings → Security → App Passwords).
ZOHO_IMAP_HOST = os.getenv("FTA_ZOHO_IMAP_HOST", "imap.zoho.com")
ZOHO_IMAP_PORT = int(os.getenv("FTA_ZOHO_IMAP_PORT", "993"))
ZOHO_IMAP_USERNAME = os.getenv("FTA_ZOHO_IMAP_USERNAME", "")
ZOHO_IMAP_PASSWORD = os.getenv("FTA_ZOHO_IMAP_PASSWORD", "")
# SMTP — used by emailer.sender when smtp_profile="zoho" for manual/custom
# batches (e.g. 孚盟 customs-data outreach). Direct SSL on 465 → USE_TLS=false.
ZOHO_SMTP_HOST = os.getenv("FTA_ZOHO_SMTP_HOST", "smtppro.zoho.com")
ZOHO_SMTP_PORT = int(os.getenv("FTA_ZOHO_SMTP_PORT", "465"))
ZOHO_SMTP_USERNAME = os.getenv("FTA_ZOHO_SMTP_USERNAME", "")
ZOHO_SMTP_PASSWORD = os.getenv("FTA_ZOHO_SMTP_PASSWORD", "")
ZOHO_SMTP_USE_TLS = os.getenv("FTA_ZOHO_SMTP_USE_TLS", "false").lower() == "true"
ZOHO_SMTP_FROM_EMAIL = os.getenv("FTA_ZOHO_SMTP_FROM_EMAIL", ZOHO_SMTP_USERNAME)
ZOHO_SMTP_FROM_NAME = os.getenv("FTA_ZOHO_SMTP_FROM_NAME", SENDER_NAME)

# Sent-follow-up tracker scheduler gating / window.
ZOHO_SENT_ENABLED = os.getenv("FTA_ZOHO_SENT_ENABLED", "false").lower() == "true"
ZOHO_SENT_HOUR = int(os.getenv("FTA_ZOHO_SENT_HOUR", "18"))  # after work hours
ZOHO_SENT_WINDOW_DAYS = int(os.getenv("FTA_ZOHO_SENT_WINDOW_DAYS", "1"))  # 1 = today
ZOHO_SENT_MAX_EMAILS = int(os.getenv("FTA_ZOHO_SENT_MAX_EMAILS", "50"))  # scan cap

# Daily Zoho 孚盟 batch — sends the customs-data pools (孚盟数据 high-quality
# first, then 孚盟数据普通) through the personal Zoho mailbox, capped by the
# independent Zoho 20/day quota (mailbox_quotas). Runs on weekdays and is
# fully independent of the NetEase 200/day batch, so the two channels can run
# concurrently. Staggered earlier than the NetEase batch (EMAIL_SCHEDULE_HOUR).
ZOHO_BATCH_ENABLED = os.getenv("FTA_ZOHO_BATCH_ENABLED", "false").lower() == "true"
ZOHO_BATCH_HOUR = int(os.getenv("FTA_ZOHO_BATCH_HOUR", "9"))

# ── Anti-Spam Limits ────────────────────────────────────────────────────

DAILY_EMAIL_LIMIT = int(os.getenv("FTA_DAILY_EMAIL_LIMIT", "200"))

# Zoho personal mailbox has its OWN daily quota, tracked separately from the
# NetEase batch quota (mailbox_quotas table). Keep low to protect the mailbox
# from being flagged as spam.
ZOHO_DAILY_LIMIT = int(os.getenv("FTA_ZOHO_DAILY_LIMIT", "20"))

# Tiered per-domain limits (replaces flat PER_DOMAIN_DAILY_LIMIT)
# Free email providers (gmail/hotmail/yahoo/etc.) — shared infrastructure, higher limit
FREE_EMAIL_DOMAIN_LIMIT = int(os.getenv("FTA_FREE_EMAIL_DOMAIN_LIMIT", "25"))
# Business/company domains — dedicated servers, lower limit per org
BUSINESS_DOMAIN_LIMIT = int(os.getenv("FTA_BUSINESS_DOMAIN_LIMIT", "15"))
# Legacy flat limit kept for backward compatibility
PER_DOMAIN_DAILY_LIMIT = int(os.getenv("FTA_PER_DOMAIN_LIMIT", "5"))

MIN_SEND_INTERVAL_SEC = int(os.getenv("FTA_MIN_SEND_INTERVAL", "20"))
MAX_SEND_INTERVAL_SEC = int(os.getenv("FTA_MAX_SEND_INTERVAL", "90"))

# ── Scheduler ───────────────────────────────────────────────────────────

SCRAPE_SCHEDULE_HOUR = int(os.getenv("FTA_SCRAPE_HOUR", "8"))  # fallback fixed hour
# Follow-up check runs after IMAP check — see collector/scheduler.py
FOLLOWUP_CHECK_HOUR = int(os.getenv("FTA_FOLLOWUP_HOUR", "14"))  # 2 PM, after morning collection+batch+IMAP
# Preserve the existing follow-up scheduler by default.  P13 operators can
# explicitly hold it while validating first-contact delivery and bounce flow.
FOLLOWUP_SCHEDULE_ENABLED = os.getenv(
    "FTA_FOLLOWUP_SCHEDULE_ENABLED", "false"
).lower() == "true"

# Daily email batch (runs after collection window)
EMAIL_SCHEDULE_ENABLED = os.getenv("FTA_EMAIL_SCHEDULE_ENABLED", "false").lower() == "true"
EMAIL_SCHEDULE_HOUR = int(os.getenv("FTA_EMAIL_SCHEDULE_HOUR", "10"))
EMAIL_BATCH_SIZE = int(os.getenv("FTA_EMAIL_BATCH_SIZE", "200"))
# Optional YYYY-MM-DD boundary for a controlled trial.  It prevents a restart
# from using the scheduler's catch-up path before the operator's chosen start.
EMAIL_SCHEDULE_START_DATE = os.getenv("FTA_EMAIL_SCHEDULE_START_DATE", "").strip()

# MX pre-filter: when selecting a batch, fetch limit × this factor candidates,
# keep only the highest-priority ones whose email domain passes the MX check,
# and send exactly `limit` of them. An overscan factor lets the selector compensate
# for ineligible domains. MX results are cached per domain for one hour.
MX_OVERSCAN_FACTOR = int(os.getenv("FTA_MX_OVERSCAN_FACTOR", "8"))

# Daily IMAP inbox check (runs after email batch — detects replies and bounces)
IMAP_CHECK_ENABLED = os.getenv("FTA_IMAP_CHECK_ENABLED", "false").lower() == "true"
IMAP_CHECK_HOUR = int(os.getenv("FTA_IMAP_CHECK_HOUR", "11"))
IMAP_CHECK_INTERVAL_HOURS = int(os.getenv("FTA_IMAP_CHECK_INTERVAL", "3"))  # also run every N hours during business hours

# P13 normal-outbound control.  It is fail-closed by default: normal delivery
# needs the exact operator-owned flag *and* the configured IMAP monitor that
# detects asynchronous DSN bounces.  It does not change a quota, bypass
# SendingGuard, or select a self-hosted transport.
_OUTBOUND_DELIVERY_REQUESTED = (
    os.getenv("FTA_OUTBOUND_DELIVERY_ENABLED", "false").lower() == "true"
)
OUTBOUND_DELIVERY_ENABLED = bool(
    _OUTBOUND_DELIVERY_REQUESTED
    and IMAP_CHECK_ENABLED
    and IMAP_USERNAME
    and IMAP_PASSWORD
)

# P10's simulation seam remains the default.  P12's protected single-delivery
# runner temporarily changes this process-local value, while P13's normal
# operator control makes it false only after the IMAP prerequisite is met.
EMAIL_DRY_RUN = not OUTBOUND_DELIVERY_ENABLED

# Randomized scheduling window (daily collection picks a random time in this range)
SCHEDULE_TIME_WINDOW_START = int(os.getenv("FTA_SCHEDULE_WINDOW_START", "8"))
SCHEDULE_TIME_WINDOW_START_MINUTE = int(os.getenv("FTA_SCHEDULE_WINDOW_START_MINUTE", "5"))
SCHEDULE_TIME_WINDOW_END = int(os.getenv("FTA_SCHEDULE_WINDOW_END", "10"))

# Segmented collection: how many countries to collect per day
# Set to 0 to collect ALL countries (no segmentation)
COLLECTION_SEGMENT_SIZE = int(os.getenv("FTA_SEGMENT_SIZE", "0"))

# Full keyword scan interval: how often to run the full country×keyword matrix.
# Every N days: full 207×25 (now 40 with Tier 4) keyword scan.
# On off-days: unfiltered-only scan (207 queries) to catch new registrations.
# Set to 1 to run full scan every day (legacy behavior).
# Set to 0 to disable full keyword scan entirely (incremental-only).
COLLECTION_FULL_SCAN_INTERVAL_DAYS = int(os.getenv("FTA_FULL_SCAN_INTERVAL_DAYS", "3"))

# On incremental (off-full-scan) days, scan the Tier4 long-tail keywords
# (unfiltered + Tier4) instead of the unfiltered-only scan, which returns the
# same ~7.8k stale leads and ~0 new. Tier4 source of truth:
# collector/sources/xunkexing.py FASTENER_KEYWORDS_TIER4.
# NOTE 2026-08-08: keyword searches return SUBSETS of the deep unfiltered query
# and English keywords match ~0 non-English companies (e.g. germany bolt=0),
# so keywords are now OFF by default — deep unfiltered pagination is primary.
COLLECTION_INCREMENTAL_KEYWORDS_ENABLED = os.getenv("FTA_INCREMENTAL_KEYWORDS", "false").lower() == "true"

# Max leads to fetch per country×keyword query.
# Higher = deeper pagination, more leads, longer runtime.
# Default 1000 = up to 20 pages per country; big industrial countries hit this
# cap, small countries end on an empty page.
COLLECTION_MAX_PER_COUNTRY = int(os.getenv("FTA_MAX_PER_COUNTRY", "1000"))

# Hard safety cap on pages per country×keyword query (defense-in-depth against
# pathological pagination). 100 pages × 50/page = 5000 leads max per query.
COLLECTION_MAX_PAGES_PER_QUERY = int(os.getenv("FTA_MAX_PAGES_PER_QUERY", "100"))

# Require an email for a lead to be ingested (2026-08-08).
# 无邮箱线索对邮件外联无用且曾占库 57k+ 条 —— 之后不再入库。
# Set False to allow no-email leads (e.g. phone/WhatsApp-only collection).
COLLECTION_REQUIRE_EMAIL = os.getenv("FTA_REQUIRE_EMAIL", "true").lower() == "true"

# Keyword search mode: when True, queries each country with 24 fastener-relevant
# keywords (bolt, screw, nut, washer, fastener, rivet, hardware, etc.)
# Each keyword returns DIFFERENT results — this is the primary scaling mechanism.
# The API's industry_id filter does NOT work (returns identical results).
# NOTE 2026-08-08: deprecated as PRIMARY — the deep unfiltered query subsumes
# keyword coverage (keywords only add English-matched subsets) and the keyword
# matrix was also capped at page-1. Kept available for explicit opt-in.
COLLECTION_USE_KEYWORD_SEARCH = os.getenv("FTA_USE_KEYWORD_SEARCH", "false").lower() == "true"

# Use all 207 countries (instead of curated 24)
COLLECTION_USE_ALL_COUNTRIES = os.getenv("FTA_USE_ALL_COUNTRIES", "true").lower() == "true"

# DEPRECATED: industry_id filter is non-functional on this API
# COLLECTION_USE_INDUSTRY_TIERS kept for backward compat — maps to keyword search
COLLECTION_USE_INDUSTRY_TIERS = os.getenv("FTA_USE_INDUSTRY_TIERS", "false").lower() == "true"

# How many days of health data to keep
MONITOR_RETENTION_DAYS = int(os.getenv("FTA_MONITOR_RETENTION", "30"))

# ── Saturday self-check job (每周六自查: 审计 + 回归测试 + 清理 + 周报) ──────
SATURDAY_CHECK_ENABLED = os.getenv("FTA_SATURDAY_CHECK_ENABLED", "true").lower() == "true"
SATURDAY_CHECK_HOUR = int(os.getenv("FTA_SATURDAY_CHECK_HOUR", "9"))
# Dated log files older than this many days are deleted by clean_logs().
LOG_RETENTION_DAYS = int(os.getenv("FTA_LOG_RETENTION_DAYS", "30"))

# ── Scraping ────────────────────────────────────────────────────────────

SCRAPE_USER_AGENT = os.getenv(
    "FTA_SCRAPE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
SCRAPE_REQUEST_DELAY = float(os.getenv("FTA_SCRAPE_DELAY", "2.0"))  # seconds

# ── Rate Limiting (risk control) ───────────────────────────────────────

# Max API requests per minute (token bucket ceiling)
RATE_LIMIT_REQUESTS_PER_MINUTE = int(os.getenv("FTA_RATE_LIMIT_RPM", "20"))

# Minimum delay between requests (seconds) — base value before jitter
RATE_LIMIT_MIN_DELAY = float(os.getenv("FTA_RATE_LIMIT_MIN_DELAY", "1.5"))

# Maximum adaptive delay after repeated errors (seconds)
RATE_LIMIT_MAX_DELAY = float(os.getenv("FTA_RATE_LIMIT_MAX_DELAY", "30.0"))

# Jitter: randomize delay by +/- this factor (0.3 = ±30%)
RATE_LIMIT_JITTER = float(os.getenv("FTA_RATE_LIMIT_JITTER", "0.3"))

# Backoff multiplier on each error
RATE_LIMIT_BACKOFF_MULTIPLIER = float(os.getenv("FTA_RATE_LIMIT_BACKOFF", "1.5"))

# Consecutive errors before circuit breaker opens
RATE_LIMIT_MAX_CONSECUTIVE_ERRORS = int(os.getenv("FTA_RATE_LIMIT_MAX_ERRORS", "8"))

# Circuit breaker cooldown (seconds) after opening
RATE_LIMIT_CIRCUIT_COOLDOWN = float(os.getenv("FTA_RATE_LIMIT_COOLDOWN", "300.0"))

# ── Retry (risk control) ───────────────────────────────────────────────

# Max retry attempts per request (total tries = 1 + this)
RETRY_MAX_ATTEMPTS = int(os.getenv("FTA_RETRY_MAX", "3"))

# Base delay before first retry (seconds)
RETRY_BASE_DELAY = float(os.getenv("FTA_RETRY_BASE_DELAY", "2.0"))

# Maximum retry delay after exponential backoff (seconds)
RETRY_MAX_DELAY = float(os.getenv("FTA_RETRY_MAX_DELAY", "60.0"))

# Retry backoff factor (delay *= factor each attempt)
RETRY_BACKOFF_FACTOR = float(os.getenv("FTA_RETRY_BACKOFF", "2.0"))

# ── Facebook Collection ──────────────────────────────────────────────────

# Source lifecycle controls.  Facebook and XunKeXing are paused by default:
# this showcase ships no external sessions or recipient lists. Adapters
# can be enabled explicitly in a separate operator-configured environment.
COLLECTION_INDUSTRY_ENABLED = os.getenv("FTA_INDUSTRY_COLLECTION_ENABLED", "false").lower() == "true"
COLLECTION_XUNKEXING_ENABLED = os.getenv("FTA_XUNKEXING_COLLECTION_ENABLED", "false").lower() == "true"
COLLECTION_LINKEDIN_ENABLED = os.getenv("FTA_LINKEDIN_COLLECTION_ENABLED", "false").lower() == "true"

# Existing XunKeXing leads are retained unchanged, but are excluded from the
# default batch candidate pool while the source is archived.  An explicit
# ``source=`` batch remains possible for an operator-led review.
OUTREACH_ARCHIVED_SOURCES = frozenset({"xunkexing"})

# Facebook requires a logged-in session (setup once via: python -m collector.setup facebook)
FB_COLLECTION_ENABLED = os.getenv("FTA_FB_COLLECTION_ENABLED", "false").lower() == "true"
FB_COLLECTION_HOUR = int(os.getenv("FTA_FB_COLLECTION_HOUR", "9"))
# Targeted mode visits curated trade groups directly (higher yield than broad
# search, which surfaces few public emails). Broad mode is the old behavior.
FB_COLLECTION_MODE = os.getenv("FTA_FB_COLLECTION_MODE", "targeted")
# 3-day interval: FB's anti-bot reacts to daily automation; targeted groups are
# stable so less frequent runs lose little.
FB_COLLECTION_INTERVAL_DAYS = int(os.getenv("FTA_FB_COLLECTION_INTERVAL_DAYS", "3"))
FB_COLLECTION_MAX_LEADS = int(os.getenv("FTA_FB_COLLECTION_MAX_LEADS", "80"))

# ── Optional industry translation adapter (disabled in the portfolio demo) ───────────────────────

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

# ── WhatsApp (Twilio — Phase 3) ─────────────────────────────────────────

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")  # +14155238886

# ── Logging ─────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
