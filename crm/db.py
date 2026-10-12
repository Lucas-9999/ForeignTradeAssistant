"""
CRM Database Builder — creates and initializes the SQLite database.

Pattern: mirrors PriceGod's etl/db_builder.py — WAL mode, CREATE TABLE IF NOT EXISTS,
comprehensive indexes, idempotent initialization.

Phase 1 tables:
- leads              — prospects from scraping or manual entry
- customers          — confirmed business contacts
- email_templates    — reusable Jinja2 email templates
- communications     — all outbound/inbound message records
- campaigns          — batch outreach campaigns
- blacklist          — bounced/unsubscribed emails
- sending_quotas     — daily anti-spam rate limiter
- scraping_tasks     — crawl job log (Phase 2)
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════════════════

# 当前 schema 版本（盖章于 PRAGMA user_version）。CRM-004：普通连接不再隐式
# 迁移；新增 DDL 必须 bump 此值并补一步幂等迁移，再用 tools/migrate_db.py
# 显式升级（前置：备份 + integrity_check）。
SCHEMA_VERSION = 14

DB_SCHEMA = """
-- ═══ Leads: prospects discovered via scraping or manual entry ═══
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT,
    contact_name TEXT,
    email TEXT,
    phone TEXT,
    whatsapp TEXT,
    website TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    source_url TEXT,
    source_detail TEXT,
    industry TEXT,
    country TEXT,
    notes TEXT,
    status TEXT DEFAULT 'new',
    duplicate_of INTEGER REFERENCES leads(id),
    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    first_contacted_at TIMESTAMP,
    last_contacted_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_name);
CREATE INDEX IF NOT EXISTS idx_leads_source ON leads(source);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_country ON leads(country);
CREATE INDEX IF NOT EXISTS idx_leads_industry ON leads(industry);
CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at);

-- ═══ Customers: confirmed business contacts ═══
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id),
    company_name TEXT NOT NULL,
    contact_name TEXT,
    email TEXT,
    phone TEXT,
    whatsapp TEXT,
    country TEXT,
    industry TEXT,
    preferred_channel TEXT DEFAULT 'email',
    followup_interval_days INTEGER DEFAULT 30,
    last_contacted_at TIMESTAMP,
    next_followup_at TIMESTAMP,
    total_orders INTEGER DEFAULT 0,
    notes TEXT,
    tags TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
CREATE INDEX IF NOT EXISTS idx_customers_company ON customers(company_name);
CREATE INDEX IF NOT EXISTS idx_customers_next_followup ON customers(next_followup_at);
CREATE INDEX IF NOT EXISTS idx_customers_active ON customers(is_active);

-- ═══ Email Templates: Jinja2 templates with rotation tracking ═══
CREATE TABLE IF NOT EXISTS email_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    subject_template TEXT NOT NULL,
    body_template TEXT NOT NULL,
    description TEXT,
    category TEXT DEFAULT 'icebreaker',
    language TEXT DEFAULT 'en',
    usage_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMP,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_templates_category ON email_templates(category);
CREATE INDEX IF NOT EXISTS idx_templates_active ON email_templates(is_active);

-- ═══ Communications: all outbound/inbound messages ═══
CREATE TABLE IF NOT EXISTS communications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER REFERENCES leads(id),
    customer_id INTEGER REFERENCES customers(id),
    template_id INTEGER REFERENCES email_templates(id),
    channel TEXT NOT NULL DEFAULT 'email',
    direction TEXT NOT NULL DEFAULT 'outbound',
    subject TEXT,
    body TEXT,
    status TEXT DEFAULT 'pending',
    error_message TEXT,
    message_id TEXT,              -- RFC 2822 Message-ID of sent email
    in_reply_to TEXT,             -- Message-ID this message replies to
    references_header TEXT,       -- Threading References chain
    imap_uid TEXT,                -- IMAP UID to prevent re-processing
    source TEXT DEFAULT 'system', -- provenance: 'system' (auto) / 'zoho' (manual)
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered_at TIMESTAMP,
    replied_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_comm_lead ON communications(lead_id);
CREATE INDEX IF NOT EXISTS idx_comm_customer ON communications(customer_id);
CREATE INDEX IF NOT EXISTS idx_comm_status ON communications(status);
CREATE INDEX IF NOT EXISTS idx_comm_sent ON communications(sent_at);
CREATE INDEX IF NOT EXISTS idx_comm_channel ON communications(channel);
CREATE INDEX IF NOT EXISTS idx_comm_message_id ON communications(message_id);
CREATE INDEX IF NOT EXISTS idx_comm_imap_uid ON communications(imap_uid);

-- ═══ Campaigns: batch outreach operations ═══
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    template_id INTEGER REFERENCES email_templates(id),
    target_filter TEXT,
    status TEXT DEFAULT 'draft',
    scheduled_at TIMESTAMP,
    completed_at TIMESTAMP,
    total_sent INTEGER DEFAULT 0,
    total_bounced INTEGER DEFAULT 0,
    total_replied INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);

-- ═══ Blacklist: never-send addresses ═══
CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    reason TEXT DEFAULT 'bounced',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_blacklist_email ON blacklist(email);

-- ═══ Sending Quotas: daily rate limit tracking ═══
CREATE TABLE IF NOT EXISTS sending_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    email_count INTEGER DEFAULT 0,
    domain_counts TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_quotas_date ON sending_quotas(date);

-- ═══ Per-mailbox sending quotas (Zoho vs NetEase) ═══
-- sending_quotas above stays the NetEase/default batch counter.
-- mailbox_quotas tracks other mailboxes (currently the personal Zoho
-- mailbox) so its 20/day cap is independent of the NetEase 200/day budget.
CREATE TABLE IF NOT EXISTS mailbox_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    mailbox TEXT NOT NULL DEFAULT 'default',
    email_count INTEGER DEFAULT 0,
    domain_counts TEXT DEFAULT '{}',
    UNIQUE(date, mailbox)
);

CREATE INDEX IF NOT EXISTS idx_mailbox_quotas_date ON mailbox_quotas(date);

-- ═══ Scraping Tasks: crawl job log (Phase 2) ═══
CREATE TABLE IF NOT EXISTS scraping_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    leads_found INTEGER DEFAULT 0,
    leads_new INTEGER DEFAULT 0,
    error_message TEXT,
    search_mode TEXT,
    pid INTEGER,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scrape_source ON scraping_tasks(source);
CREATE INDEX IF NOT EXISTS idx_scrape_status ON scraping_tasks(status);

-- ═══ Collection Health: per-run metrics (Phase 2 — risk control) ═══
CREATE TABLE IF NOT EXISTS collection_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES scraping_tasks(id),
    source TEXT NOT NULL,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    avg_latency_ms REAL DEFAULT 0,
    max_latency_ms REAL DEFAULT 0,
    total_requests INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    rate_limit_count INTEGER DEFAULT 0,
    circuit_breaker_hits INTEGER DEFAULT 0,
    countries_total INTEGER DEFAULT 0,
    countries_collected INTEGER DEFAULT 0,
    countries_failed INTEGER DEFAULT 0,
    leads_found INTEGER DEFAULT 0,
    leads_new INTEGER DEFAULT 0,
    health_score REAL DEFAULT 100.0,
    details TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_health_source ON collection_health(source);
CREATE INDEX IF NOT EXISTS idx_health_started ON collection_health(started_at);

-- ═══ Collection Segments: country rotation tracking ═══
CREATE TABLE IF NOT EXISTS collection_segments (
    country_key TEXT PRIMARY KEY,
    last_collected_at TIMESTAMP,
    collection_count INTEGER DEFAULT 0,
    last_leads_found INTEGER DEFAULT 0,
    consecutive_errors INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_segments_last ON collection_segments(last_collected_at);

-- ═══ Batch Run Guards: 每日批次互斥（V9，修复 08-17 双批次并发） ═══
-- 主键 (kind, date)：每个发送种类每天至多一条记录。status 从 'running'
-- 到 'done' 持久化——采集 finally 链、cron、启动 catch-up 三个入口在
-- 同一天只会有一个批次真正运行，其余入口 claim 失败后仅记日志跳过。
CREATE TABLE IF NOT EXISTS batch_run_guards (
    kind TEXT NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    PRIMARY KEY (kind, date)
);
"""


REPUTATION_SCHEMA = """
-- ═══ V10 Email Reputation P1: persistence only, no sending behavior ═══
CREATE TABLE IF NOT EXISTS email_suppressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT,
    domain TEXT,
    reason TEXT NOT NULL CHECK (
        reason IN ('hard_bounce', 'unsubscribe', 'spam_complaint',
                   'invalid_email', 'dead_domain', 'manual_block',
                   'do_not_contact')
    ),
    source TEXT NOT NULL DEFAULT 'system',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    is_permanent INTEGER NOT NULL DEFAULT 1 CHECK (is_permanent IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK (is_permanent = 1 OR (expires_at IS NOT NULL AND datetime(expires_at) IS NOT NULL)),
    CHECK (
        (email IS NOT NULL AND TRIM(email) <> '') OR
        (domain IS NOT NULL AND TRIM(domain) <> '')
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_suppressions_email
    ON email_suppressions(LOWER(email))
    WHERE email IS NOT NULL AND TRIM(email) <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_email_suppressions_domain
    ON email_suppressions(LOWER(domain))
    WHERE email IS NULL AND domain IS NOT NULL AND TRIM(domain) <> '';
CREATE INDEX IF NOT EXISTS idx_email_suppressions_reason
    ON email_suppressions(reason);
CREATE INDEX IF NOT EXISTS idx_email_suppressions_expires
    ON email_suppressions(expires_at);

CREATE TABLE IF NOT EXISTS reputation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    old_score REAL CHECK (old_score IS NULL OR (old_score >= 0 AND old_score <= 100)),
    new_score REAL CHECK (new_score IS NULL OR (new_score >= 0 AND new_score <= 100)),
    reason TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_reputation_events_entity
    ON reputation_events(entity_type, entity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reputation_events_severity
    ON reputation_events(severity, created_at);

CREATE TABLE IF NOT EXISTS mailbox_health (
    mailbox TEXT PRIMARY KEY REFERENCES mailboxes(name) ON DELETE CASCADE,
    health_score REAL NOT NULL DEFAULT 100 CHECK (health_score >= 0 AND health_score <= 100),
    status TEXT NOT NULL DEFAULT 'healthy'
        CHECK (status IN ('healthy', 'warning', 'restricted', 'paused', 'warming', 'disabled')),
    daily_sent INTEGER NOT NULL DEFAULT 0 CHECK (daily_sent >= 0),
    hourly_sent INTEGER NOT NULL DEFAULT 0 CHECK (hourly_sent >= 0),
    hard_bounce_rate REAL NOT NULL DEFAULT 0 CHECK (hard_bounce_rate >= 0 AND hard_bounce_rate <= 1),
    soft_bounce_rate REAL NOT NULL DEFAULT 0 CHECK (soft_bounce_rate >= 0 AND soft_bounce_rate <= 1),
    reply_rate REAL NOT NULL DEFAULT 0 CHECK (reply_rate >= 0 AND reply_rate <= 1),
    complaint_rate REAL NOT NULL DEFAULT 0 CHECK (complaint_rate >= 0 AND complaint_rate <= 1),
    last_error TEXT,
    last_health_check TIMESTAMP,
    provider TEXT NOT NULL,
    sending_ip TEXT,
    domain TEXT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_mailbox_health_status
    ON mailbox_health(status, health_score);
CREATE INDEX IF NOT EXISTS idx_mailbox_health_domain
    ON mailbox_health(domain);

CREATE TABLE IF NOT EXISTS domain_health (
    domain TEXT PRIMARY KEY COLLATE NOCASE,
    health_score REAL NOT NULL DEFAULT 100 CHECK (health_score >= 0 AND health_score <= 100),
    status TEXT NOT NULL DEFAULT 'healthy'
        CHECK (status IN ('healthy', 'warning', 'restricted', 'paused', 'warming', 'disabled')),
    mx_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (mx_status IN ('PASS', 'WARNING', 'FAIL', 'UNKNOWN')),
    spf_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (spf_status IN ('PASS', 'WARNING', 'FAIL', 'UNKNOWN')),
    dkim_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (dkim_status IN ('PASS', 'WARNING', 'FAIL', 'UNKNOWN')),
    dmarc_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (dmarc_status IN ('PASS', 'WARNING', 'FAIL', 'UNKNOWN')),
    a_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (a_status IN ('PASS', 'WARNING', 'FAIL', 'UNKNOWN')),
    ptr_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (ptr_status IN ('PASS', 'WARNING', 'FAIL', 'UNKNOWN')),
    tls_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (tls_status IN ('PASS', 'WARNING', 'FAIL', 'UNKNOWN')),
    sending_ip TEXT,
    authentication_status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (authentication_status IN ('PASS', 'WARNING', 'FAIL', 'UNKNOWN')),
    last_checked TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_domain_health_status
    ON domain_health(status, health_score);

CREATE TABLE IF NOT EXISTS recipient_domain_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL COLLATE NOCASE,
    metric_date TEXT NOT NULL,
    metric_hour INTEGER NOT NULL CHECK (metric_hour >= 0 AND metric_hour <= 23),
    mx_provider TEXT,
    hourly_sent INTEGER NOT NULL DEFAULT 0 CHECK (hourly_sent >= 0),
    daily_sent INTEGER NOT NULL DEFAULT 0 CHECK (daily_sent >= 0),
    bounce_rate REAL NOT NULL DEFAULT 0 CHECK (bounce_rate >= 0 AND bounce_rate <= 1),
    deferral_rate REAL NOT NULL DEFAULT 0 CHECK (deferral_rate >= 0 AND deferral_rate <= 1),
    last_smtp_error TEXT,
    risk_level TEXT NOT NULL DEFAULT 'low'
        CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(domain, metric_date, metric_hour)
);
CREATE INDEX IF NOT EXISTS idx_recipient_domain_metrics_period
    ON recipient_domain_metrics(metric_date, metric_hour);
CREATE INDEX IF NOT EXISTS idx_recipient_domain_metrics_risk
    ON recipient_domain_metrics(risk_level, metric_date);
"""

REPUTATION_INVARIANTS_SCHEMA = """
-- ═══ V11-V12 Email Reputation P1: raw-SQL safety invariants ═══
CREATE TRIGGER IF NOT EXISTS trg_email_suppressions_validate_insert
BEFORE INSERT ON email_suppressions
WHEN NEW.is_permanent = 0 AND
     (NEW.expires_at IS NULL OR datetime(NEW.expires_at) IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'temporary suppression requires a valid expiry');
END;

CREATE TRIGGER IF NOT EXISTS trg_email_suppressions_validate_update
BEFORE UPDATE ON email_suppressions
WHEN NEW.is_permanent = 0 AND
     (NEW.expires_at IS NULL OR datetime(NEW.expires_at) IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'temporary suppression requires a valid expiry');
END;

CREATE TRIGGER IF NOT EXISTS trg_email_suppressions_reason_insert
BEFORE INSERT ON email_suppressions
WHEN NEW.reason NOT IN
        ('hard_bounce', 'unsubscribe', 'spam_complaint', 'invalid_email',
         'dead_domain', 'manual_block', 'do_not_contact')
BEGIN
    SELECT RAISE(ABORT, 'invalid suppression reason');
END;

CREATE TRIGGER IF NOT EXISTS trg_email_suppressions_reason_update
BEFORE UPDATE ON email_suppressions
WHEN NEW.reason NOT IN
        ('hard_bounce', 'unsubscribe', 'spam_complaint', 'invalid_email',
         'dead_domain', 'manual_block', 'do_not_contact')
BEGIN
    SELECT RAISE(ABORT, 'invalid suppression reason');
END;

CREATE TRIGGER IF NOT EXISTS trg_reputation_events_validate_insert
BEFORE INSERT ON reputation_events
WHEN NEW.entity_type NOT IN
        ('domain', 'mailbox', 'sending_ip', 'provider', 'recipient_domain') OR
     NEW.event_type NOT IN
        ('DKIM_FAILED', 'HARD_BOUNCE_SPIKE', 'RATE_LIMITED',
         'MAILBOX_PAUSED', 'MAILBOX_RECOVERED', 'MAILBOX_HEALTH_CHANGED',
         'DOMAIN_HEALTH_CHANGED', 'PTR_FAILED', 'DMARC_FAILED') OR
     NEW.severity NOT IN ('info', 'warning', 'critical')
BEGIN
    SELECT RAISE(ABORT, 'invalid reputation event type');
END;

CREATE TRIGGER IF NOT EXISTS trg_reputation_events_immutable_update
BEFORE UPDATE ON reputation_events
BEGIN
    SELECT RAISE(ABORT, 'reputation events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_reputation_events_immutable_delete
BEFORE DELETE ON reputation_events
BEGIN
    SELECT RAISE(ABORT, 'reputation events are append-only');
END;
"""

DELIVERY_AUTHORIZATION_SCHEMA = """
-- ═══ V13 P11: one-time, exact-scope delivery authorization (no transport) ═══
CREATE TABLE IF NOT EXISTS delivery_authorizations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    mailbox TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('controlled_test', 'manual_verification')),
    expires_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approval_note TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'revoked', 'consumed')),
    closed_at TEXT,
    closed_by TEXT,
    delivery_attempt_id TEXT,
    CHECK (expires_at > created_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_authorizations_active_scope
    ON delivery_authorizations(lead_id, email, mailbox, purpose)
    WHERE status = 'active';
"""

DELIVERY_AUTHORIZATION_INVARIANTS_SCHEMA = """
-- ═══ V14 P11: authorization scope is immutable after approval ════════════
CREATE TRIGGER IF NOT EXISTS trg_delivery_authorizations_scope_immutable
BEFORE UPDATE OF lead_id, email, mailbox, purpose, expires_at ON delivery_authorizations
BEGIN
    SELECT RAISE(ABORT, 'delivery authorization scope is immutable');
END;
"""

DB_SCHEMA += (
    REPUTATION_SCHEMA
    + REPUTATION_INVARIANTS_SCHEMA
    + DELIVERY_AUTHORIZATION_SCHEMA
    + DELIVERY_AUTHORIZATION_INVARIANTS_SCHEMA
)


# ═══════════════════════════════════════════════════════════════════════════
# DEFAULT SEED DATA
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_TEMPLATES = [{'name': 'icebreaker_general',
  'subject_template': "Quick question about {{ company_name }}'s fastener supply",
  'body_template': '<html><body><p>Hi {{ contact_name | default("Demo contact", true) '
                   '}},</p><p>This is a synthetic preview for {{ company_name }}. It illustrates a '
                   'configurable workflow and contains no real customer or company '
                   'information.</p><p>No message is sent by the read-only portfolio '
                   'demo.</p><p>Example Team<br>Example '
                   'Operations<br>sender@portfolio.invalid</p></body></html>',
  'category': 'icebreaker',
  'description': 'Generic first-contact email introducing fastener product range'},
 {'name': 'icebreaker_short',
  'subject_template': 'Fastener supplier for {{ company_name }}',
  'body_template': '<html><body><p>Hi {{ contact_name | default("Demo contact", true) '
                   '}},</p><p>This is a synthetic preview for {{ company_name }}. It illustrates a '
                   'configurable workflow and contains no real customer or company '
                   'information.</p><p>No message is sent by the read-only portfolio '
                   'demo.</p><p>Example Team<br>Example '
                   'Operations<br>sender@portfolio.invalid</p></body></html>',
  'category': 'icebreaker',
  'description': 'Short, casual icebreaker for initial outreach'},
 {'name': 'followup_30day',
  'subject_template': 'Following up — fasteners for {{ company_name }}',
  'body_template': '<html><body><p>Hi {{ contact_name | default("Demo contact", true) '
                   '}},</p><p>This is a synthetic preview for {{ company_name }}. It illustrates a '
                   'configurable workflow and contains no real customer or company '
                   'information.</p><p>No message is sent by the read-only portfolio '
                   'demo.</p><p>Example Team<br>Example '
                   'Operations<br>sender@portfolio.invalid</p></body></html>',
  'category': 'followup',
  'description': "30-day follow-up for leads that haven't replied"},
 {'name': 'followup_gentle',
  'subject_template': 'Quick follow-up — fastener catalog for {{ company_name }}',
  'body_template': '<html><body><p>Hi {{ contact_name | default("Demo contact", true) '
                   '}},</p><p>This is a synthetic preview for {{ company_name }}. It illustrates a '
                   'configurable workflow and contains no real customer or company '
                   'information.</p><p>No message is sent by the read-only portfolio '
                   'demo.</p><p>Example Team<br>Example '
                   'Operations<br>sender@portfolio.invalid</p></body></html>',
  'category': 'followup',
  'description': 'Gentle 3-5 day follow-up offering product catalog'},
 {'name': 'followup_value',
  'subject_template': 'Industry insight — fastener trends for {{ company_name }}',
  'body_template': '<html><body><p>Hi {{ contact_name | default("Demo contact", true) '
                   '}},</p><p>This is a synthetic preview for {{ company_name }}. It illustrates a '
                   'configurable workflow and contains no real customer or company '
                   'information.</p><p>No message is sent by the read-only portfolio '
                   'demo.</p><p>Example Team<br>Example '
                   'Operations<br>sender@portfolio.invalid</p></body></html>',
  'category': 'followup',
  'description': 'Value-add follow-up (Day 10-14) sharing industry insights'},
 {'name': 'followup_final',
  'subject_template': "Last note — still interested, {{ contact_name | default('there', true) }}?",
  'body_template': '<html><body><p>Hi {{ contact_name | default("Demo contact", true) '
                   '}},</p><p>This is a synthetic preview for {{ company_name }}. It illustrates a '
                   'configurable workflow and contains no real customer or company '
                   'information.</p><p>No message is sent by the read-only portfolio '
                   'demo.</p><p>Example Team<br>Example '
                   'Operations<br>sender@portfolio.invalid</p></body></html>',
  'category': 'followup',
  'description': 'Final follow-up (Day 25-30) — last contact, leaves door open'}]


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════


def build_database(db_path: Path, overwrite: bool = False) -> sqlite3.Connection:
    """Build or initialize the FTA database.

    Args:
        db_path: Path to SQLite database file.
        overwrite: If True, drop and recreate all tables.

    Returns:
        sqlite3.Connection with WAL mode and foreign keys enabled.
    """
    if overwrite and db_path.exists():
        db_path.unlink()
        logger.info(f"Removed existing database at {db_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    # Create all tables
    conn.executescript(DB_SCHEMA)
    conn.commit()

    # ── Run schema migrations for existing databases ─────────────────────
    _migrate_schema(conn)

    # Seed default templates if the table is empty
    _seed_default_templates(conn)

    logger.info(f"Database initialized at {db_path}")
    return conn


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Get a read/write connection to the database.

    Raises FileNotFoundError if the database doesn't exist.

    NOTE (CRM-004): this does NOT run migrations. Schema is only changed by
    the explicit migrator `python tools/migrate_db.py` (after a backup).
    Applications must call ensure_schema_current() at startup to refuse stale
    schemas instead of silently auto-migrating the production DB.
    """
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database not found at {db_path}. Run build_database() first."
        )
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def get_schema_version(db_path: Path) -> int:
    """Read the schema version (PRAGMA user_version) of a database.

    A DB that was never stamped (pre-explicit-migration, or built before
    versioning) reports 0 even when all current tables exist.
    """
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def ensure_schema_current(db_path: Path) -> int:
    """Refuse to proceed when the DB schema is behind SCHEMA_VERSION (CRM-004).

    Returns the current version when it matches SCHEMA_VERSION; otherwise
    raises RuntimeError telling the operator to back up and migrate explicitly.
    """
    actual = get_schema_version(db_path)
    if actual >= SCHEMA_VERSION:
        return actual
    raise RuntimeError(
        f"数据库 schema 版本落后：当前 v{actual}，要求 v{SCHEMA_VERSION}。"
        f"请先备份（python tools/backup_db.py --label <label> --check）再迁移："
        f"python tools/migrate_db.py --target v{SCHEMA_VERSION} --label <label> --check"
    )


def get_db() -> sqlite3.Connection:
    """Open the configured DB (building it first if absent) — for API modules.

    Mirrors app.py's get_db so api/* routers can connect without importing
    app.py (which would create a circular import once app.py includes them).
    """
    import config
    path = config.DB_PATH
    if not path.exists():
        build_database(path)
    return get_connection(path)


# ═══════════════════════════════════════════════════════════════════════════
# SCHEMA MIGRATION
# ═══════════════════════════════════════════════════════════════════════════


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations and stamp PRAGMA user_version.

    Each migration is idempotent — checks if column/index exists before
    attempting to add. Version-gated (CRM-004): a DB already at
    SCHEMA_VERSION is left untouched; otherwise ALL cumulative steps run
    (V1→V8, each a no-op when already applied) and the version is stamped.
    Called by build_database (fresh installs) and tools/migrate_db.py —
    never by plain get_connection().
    """
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    # ── V1 → V2: IMAP reply-tracking columns ────────────────────────────
    existing_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info('communications')").fetchall()
    }

    v2_columns = {
        "message_id": "TEXT",
        "in_reply_to": "TEXT",
        "references_header": "TEXT",
        "imap_uid": "TEXT",
    }
    for col_name, col_type in v2_columns.items():
        if col_name not in existing_cols:
            logger.info("Migration: adding communications.%s column", col_name)
            conn.execute(
                f"ALTER TABLE communications ADD COLUMN {col_name} {col_type}"
            )

    # V2 indexes (IF NOT EXISTS is safe even on older SQLite)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comm_message_id "
        "ON communications(message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_comm_imap_uid "
        "ON communications(imap_uid)"
    )

    # ── V2 → V3: scraping_tasks.search_mode ─────────────────────────────
    # Records whether a xunkexing run was a full keyword-matrix scan
    # ('full') or an incremental unfiltered/Tier4 scan ('incremental').
    # The scheduler's full-scan-interval gating must distinguish these —
    # an unfiltered run can find 7000+ leads and was previously mistaken
    # for a full scan (leads_found >= 5000 heuristic), which prevented the
    # 40-keyword matrix from ever running again.
    st_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info('scraping_tasks')").fetchall()
    }
    if "search_mode" not in st_cols:
        logger.info("Migration: adding scraping_tasks.search_mode column")
        conn.execute("ALTER TABLE scraping_tasks ADD COLUMN search_mode TEXT")

    # ── V3 → V4: scraping_tasks.pid (owner-process tracking) ─────────────
    # Stores the PID of the process that started the crawl. On graceful
    # shutdown/exit the owner marks its OWN still-running tasks as failed
    # (PID-scoped UPDATE) so a killed collection never leaves a phantom
    # 'running' row that blocks the concurrency guard for hours. Rows created
    # before this migration have pid=NULL and fall back to age-based stale
    # cleanup only.
    st_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info('scraping_tasks')").fetchall()
    }
    if "pid" not in st_cols:
        logger.info("Migration: adding scraping_tasks.pid column")
        conn.execute("ALTER TABLE scraping_tasks ADD COLUMN pid INTEGER")

    # ── V3 → V4: communications.source (provenance) ─────────────────────
    # Marks who wrote an outbound row: 'system' (batch/follow-up automation,
    # the default) vs 'zoho' (manual personal follow-ups detected by the
    # Zoho sent tracker). Existing rows default to 'system'.
    comm_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info('communications')").fetchall()
    }
    if "source" not in comm_cols:
        logger.info("Migration: adding communications.source column")
        conn.execute(
            "ALTER TABLE communications ADD COLUMN source TEXT DEFAULT 'system'"
        )

    # ── V4 → V5: mailbox_quotas (per-mailbox anti-spam counters) ────────
    # Zoho's 20/day quota is tracked in its own row so personal-mailbox sends
    # never consume (or get consumed by) the NetEase batch budget. New table
    # only — no DDL on sending_quotas, which other processes may be writing.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mailbox_quotas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            mailbox TEXT NOT NULL DEFAULT 'default',
            email_count INTEGER DEFAULT 0,
            domain_counts TEXT DEFAULT '{}',
            UNIQUE(date, mailbox)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mailbox_quotas_date ON mailbox_quotas(date)"
    )

    # ── V5 → V6: 寻客星 buyer_id 去重索引 ──────────────────────────────
    # Expression index over source_detail JSON so create_batch/_find_duplicate
    # can dedup by the platform-stable buyer_id (added 2026-08-08).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_leads_buyer_id "
        "ON leads(json_extract(source_detail, '$.buyer_id'))"
    )
    # Partial unique index: non-empty email must be unique (email dimension is
    # already deduped by LeadManager; this is a final safety net).
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_email_nn "
        "ON leads(LOWER(email)) WHERE email IS NOT NULL AND TRIM(email) <> ''"
    )

    # ── V6 → V7: tags / groups（CRM Web Console M4 建库） ───────────────
    # 客户数据分类：tags 是灵活的逐线索标签，groups 是命名的客户分组。
    # 均为新增表（CREATE TABLE IF NOT EXISTS，幂等），不触碰 leads 主表。
    # lead_tags / group_members 随 leads/tags/groups 级联删除，不留孤儿行。
    # Blueprint 约定: customers.tags 已废弃不复用, 标签 v1 面向 leads。
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT DEFAULT 'custom',
            color TEXT DEFAULT '#64748b',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS lead_tags (
            lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
            PRIMARY KEY (lead_id, tag_id)
        );
        CREATE INDEX IF NOT EXISTS idx_lead_tags_tag ON lead_tags(tag_id);

        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            color TEXT DEFAULT '#64748b',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS group_members (
            lead_id  INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
            group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            PRIMARY KEY (lead_id, group_id)
        );
        CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);
        """
    )

    # ── V7 → V8: mailboxes（CRM Web Console M5 邮箱管理） ────────────────
    # 取代硬编码的两个 env profile。password_enc 存 Fernet 密文（FTA_MAILBOX_ENC_KEY），
    # 为 NULL 时解析器回退 env 密码（零风险：播种行 password_enc 恒为 NULL）。
    # 配额仍复用 mailbox_quotas（已按 name 键控），无需新配额表。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mailboxes (
            name TEXT PRIMARY KEY,              -- = smtp_profile / quota key
            display_name TEXT,
            smtp_host TEXT,
            smtp_port INTEGER,
            use_tls INTEGER,
            username TEXT,
            password_enc TEXT,                  -- Fernet token; NULL → env 回退
            from_email TEXT,
            from_name TEXT,
            imap_host TEXT,
            imap_port INTEGER,
            daily_limit INTEGER,                -- NULL → env 限额回退
            is_active INTEGER NOT NULL DEFAULT 1,
            source TEXT DEFAULT 'db',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )

    # env 播种：mailboxes 空 且 env 凭证非空 → 插入 default/zoho（password_enc=NULL，
    # 解析器返回与 env 字节一致的凭证 → 零行为变化）。
    _seed_env_mailboxes(conn)

    # ── V8 → V9: batch_run_guards（批次互斥，修复 08-17 双批次并发） ──────
    # 每日每 kind 至多一个活跃批次（见 DB_SCHEMA 顶部注释）。仅新增表，
    # 不触碰任何存量表/索引/数据。迁移幂等：CREATE TABLE IF NOT EXISTS。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS batch_run_guards (
            kind TEXT NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            PRIMARY KEY (kind, date)
        )"""
    )

    # ── V9 → V10: email reputation persistence layer (P1 only) ──────────
    # Adds the five approved tables. No existing column/table semantics are
    # changed, no historical rows are backfilled, and no sending path reads
    # these tables until later phases explicitly integrate the guard.
    conn.executescript(REPUTATION_SCHEMA)

    # ── V10 → V12: raw-SQL safety invariants (P1 hardening) ─────────────
    # Adds only idempotent triggers. No table rebuild, data write, quota, or
    # sending behavior changes. V12 closes the suppression reason vocabulary;
    # fresh databases also receive all constraints via DB_SCHEMA.
    conn.executescript(REPUTATION_INVARIANTS_SCHEMA)

    # ── V12 → V13: P11 authorization persistence only ──────────────────
    # This creates no authorization rows and does not alter any sending path.
    conn.executescript(DELIVERY_AUTHORIZATION_SCHEMA)

    # ── V13 → V14: P11 approval scope immutability ─────────────────────
    # Lifecycle state may close, but an approved recipient scope or expiry
    # cannot be widened/retargeted by raw SQL after creation.
    conn.executescript(DELIVERY_AUTHORIZATION_INVARIANTS_SCHEMA)

    # 盖章 schema 版本（CRM-004：此后 get_connection 不再隐式迁移）。
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _seed_env_mailboxes(conn: sqlite3.Connection) -> int:
    """V8 env seeding — populate mailboxes from env when the table is empty.

    Inserts `default` (← FTA_SMTP_*) and `zoho` (← FTA_ZOHO_SMTP_*) only when:
      1. the table is empty (idempotent — runs on every get_connection), and
      2. the env credentials are non-empty (a blank env means "nothing to seed").

    password_enc is intentionally left NULL so the resolver falls back to the
    env password — the resolved settings are byte-identical to the pre-V8
    behavior (zero behavior change). Editing the password later stores a
    Fernet token and takes precedence.

    Returns the number of rows inserted (0 or 2).
    """
    import config

    count = conn.execute("SELECT COUNT(*) FROM mailboxes").fetchone()[0]
    if count:
        return 0

    profiles = []
    if config.SMTP_USERNAME:
        profiles.append(
            (
                "default", "NetEase 163",
                config.SMTP_HOST, config.SMTP_PORT, int(config.SMTP_USE_TLS),
                config.SMTP_USERNAME, config.SMTP_FROM_EMAIL, config.SMTP_FROM_NAME,
                config.DAILY_EMAIL_LIMIT,
            )
        )
    if config.ZOHO_SMTP_USERNAME:
        profiles.append(
            (
                "zoho", "Zoho 个人邮箱",
                config.ZOHO_SMTP_HOST, config.ZOHO_SMTP_PORT, int(config.ZOHO_SMTP_USE_TLS),
                config.ZOHO_SMTP_USERNAME, config.ZOHO_SMTP_FROM_EMAIL, config.ZOHO_SMTP_FROM_NAME,
                config.ZOHO_DAILY_LIMIT,
            )
        )
    if not profiles:
        return 0

    for (name, display, host, port, use_tls, user, from_email, from_name, limit) in profiles:
        conn.execute(
            """INSERT INTO mailboxes
               (name, display_name, smtp_host, smtp_port, use_tls, username,
                password_enc, from_email, from_name, daily_limit, source)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'db')""",
            (name, display, host, port, use_tls, user, from_email, from_name, limit),
        )
    conn.commit()
    logger.info("V8 seeding: inserted %d mailbox(es) from env", len(profiles))
    return len(profiles)


# ═══════════════════════════════════════════════════════════════════════════
# SEEDING
# ═══════════════════════════════════════════════════════════════════════════


def _seed_default_templates(conn: sqlite3.Connection) -> None:
    """Insert default email templates, skipping any that already exist.

    Uses INSERT OR IGNORE so templates can be safely added to DEFAULT_TEMPLATES
    in code changes and will be backfilled on the next DB initialization.
    """
    now = conn.execute("SELECT datetime('now')").fetchone()[0]
    inserted = 0
    for tmpl in DEFAULT_TEMPLATES:
        cur = conn.execute(
            """INSERT OR IGNORE INTO email_templates
               (name, subject_template, body_template, description, category, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                tmpl["name"],
                tmpl["subject_template"],
                tmpl["body_template"],
                tmpl.get("description", ""),
                tmpl.get("category", "icebreaker"),
                now,
                now,
            ),
        )
        if cur.rowcount > 0:
            inserted += 1
    if inserted > 0:
        conn.commit()
        logger.info(f"Seeded {inserted} new default email templates")
