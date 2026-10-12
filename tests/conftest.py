"""
Test fixtures for FTA — in-memory database, test client, sample data.
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import sqlite3

from crm.db import DB_SCHEMA, build_database


@pytest.fixture(autouse=True)
def _isolate_production_db(tmp_path, monkeypatch):
    """Never let tests reach the production data/fta.db.

    Some code paths (scheduler task logging, manager helpers) open
    config.DB_PATH directly. Redirect it to a throwaway temp DB so tests
    can't write to real data — this used to leave test_source rows in the
    production scraping_tasks table.
    """
    import config

    # Only DB_PATH is redirected. DATA_DIR is left intact: module-level
    # constants (e.g. facebook.FB_PROFILE_DIR) are computed at import time
    # from config.DATA_DIR, and tests assert against that same value.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test_fta.db")
    # P10 is fail-closed in a running application.  Individual tests that
    # exercise a successful provider outcome opt into the non-sending seam;
    # P10 tests explicitly turn this back on and prove zero wire traffic.
    monkeypatch.setattr(config, "EMAIL_DRY_RUN", False, raising=False)
    # P13 is an operator-controlled production switch.  Tests must never
    # inherit a real operator's outbound setting from the loaded .env; cases
    # exercising P13 opt in explicitly at their own seam.
    monkeypatch.setattr(config, "OUTBOUND_DELIVERY_ENABLED", False, raising=False)
    yield


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite database with the full schema.

    Uses DB_SCHEMA directly (base tables) — schema migrations (_migrate_schema,
    incl. V7 tags/groups) are NOT applied here. Tests needing migrated tables
    should use the api_client fixture (build_database) instead.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(DB_SCHEMA)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """A temp-file DB with the FULL schema (build_database runs migrations),
    plus a TestClient bound to config.DB_PATH.

    Shared by the CRM API tests (M1 dashboard/leads + M4 tags/groups). Lifespan
    is NOT entered (`with`), so the collection scheduler never starts.
    """
    import config

    db_path = tmp_path / "crm_api.db"
    # Redirect BEFORE importing app so app.py's module-level captures see the
    # temp path (app.get_db now reads config at call time, belt & suspenders).
    monkeypatch.setattr(config, "DB_PATH", db_path)
    build_database(db_path)

    from fastapi.testclient import TestClient
    import app as fta_app

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """INSERT INTO leads (company_name, contact_name, email, country, source, status, discovered_at, created_at) VALUES
               ('Alpha Corp', 'Alice', 'alice@alpha.com', 'USA', 'xunkexing', 'new', datetime('now','-6 days'), datetime('now','-6 days')),
               ('Beta GmbH',  'Bob',   'bob@beta.de',     'DE',  'xunkexing', 'contacted', datetime('now','-5 days'), datetime('now','-5 days')),
               ('Gamma Ltd',  'Cara',  'cara@gamma.uk',   'UK',  'facebook',  'replied',   datetime('now','-1 days'), datetime('now','-1 days')),
               ('Delta 100%', 'Dan',   'dan@delta.com',   NULL,  'manual',    'new',       datetime('now','-1 days'), datetime('now','-1 days'));

           INSERT INTO communications (lead_id, channel, direction, subject, status, sent_at) VALUES
               (2, 'email', 'outbound', 'Hello', 'sent',  datetime('now','-4 days')),
               (3, 'email', 'outbound', 'Hi',    'sent',  datetime('now','-1 days')),
               (3, 'email', 'inbound',  'Re: Product inquiry', 'received', datetime('now'));

           INSERT INTO sending_quotas (date, email_count) VALUES (date('now'), 7);
           INSERT INTO mailbox_quotas (date, mailbox, email_count) VALUES (date('now'), 'zoho', 3);
        """
    )
    conn.commit()
    conn.close()

    return TestClient(fta_app.app)


@pytest.fixture
def sample_lead_data() -> dict:
    return {
        "company_name": "ACME Fasteners Inc.",
        "contact_name": "John Smith",
        "email": "john@acmefasteners.com",
        "phone": "+1-555-0100",
        "country": "USA",
        "industry": "Automotive",
        "source": "manual",
        "notes": "Test lead",
    }
