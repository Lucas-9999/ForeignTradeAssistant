"""
Tests for the Collector module — pipeline, base, sources, CLI.

Covers:
- Pipeline: cleaning, validation, lead mapping, ingestion
- BaseCollector: rate limiting, async context manager
- CLI: argument parsing
- Integration: end-to-end collection + CRM flow
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from collector.base import BaseCollector
from collector.pipeline import (
    clean_text,
    is_valid_email,
    infer_country_from_tld,
    infer_country_from_phone,
    normalize_website,
    raw_to_lead,
    ingest_leads,
)
from collector.cli import build_parser, build_source_registry, SOURCE_REGISTRY
from crm.manager import LeadManager
from crm.models import Lead


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline: Cleaning & Validation
# ═══════════════════════════════════════════════════════════════════════════


class TestCleanText:
    def test_normal_string(self):
        assert clean_text("  Hello World  ") == "Hello World"

    def test_none(self):
        assert clean_text(None) is None

    def test_empty_string(self):
        assert clean_text("") is None

    def test_whitespace_only(self):
        assert clean_text("   \t\n  ") is None


class TestIsValidEmail:
    def test_valid_email(self):
        assert is_valid_email("john@acmefasteners.com") is True

    def test_valid_with_subdomain(self):
        assert is_valid_email("sales@de.acmefasteners.co.uk") is True

    def test_none(self):
        assert is_valid_email(None) is False

    def test_empty(self):
        assert is_valid_email("") is False

    def test_no_at(self):
        assert is_valid_email("notanemail") is False

    def test_example_com(self):
        assert is_valid_email("test@example.com") is False

    def test_noreply(self):
        assert is_valid_email("noreply@company.com") is False

    def test_admin_generic(self):
        assert is_valid_email("admin@somewhere.com") is False

    def test_info_email(self):
        # info@ is now accepted — common at small B2B companies
        assert is_valid_email("info@acmefasteners.com") is True


class TestCountryInference:
    def test_tld_germany(self):
        assert infer_country_from_tld("https://www.acme-fasteners.de") == "Germany"

    def test_tld_uk(self):
        assert infer_country_from_tld("https://boltmaster.co.uk") == "UK"

    def test_tld_japan(self):
        assert infer_country_from_tld("https://nippon-fasteners.jp") == "Japan"

    def test_tld_ambiguous(self):
        assert infer_country_from_tld("https://example.com") is None

    def test_tld_none(self):
        assert infer_country_from_tld(None) is None

    def test_phone_germany(self):
        assert infer_country_from_phone("+49 30 123456") == "Germany"

    def test_phone_uk(self):
        assert infer_country_from_phone("+44 20 7946 0958") == "UK"

    def test_phone_us(self):
        result = infer_country_from_phone("+1 555 123 4567")
        assert result in ("USA/Canada",) or "USA" in str(result)

    def test_phone_none(self):
        assert infer_country_from_phone(None) is None


class TestNormalizeWebsite:
    def test_adds_https(self):
        assert normalize_website("example.com") == "https://example.com"

    def test_preserves_https(self):
        assert normalize_website("https://example.com") == "https://example.com"

    def test_strips_path_trailing_slash(self):
        assert normalize_website("https://example.com/about/") == "https://example.com/about"

    def test_none(self):
        assert normalize_website(None) is None


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline: raw_to_lead
# ═══════════════════════════════════════════════════════════════════════════


class TestRawToLead:
    def test_full_lead(self):
        raw = {
            "company_name": "ACME Fasteners GmbH",
            "contact_name": "Hans Mueller",
            "email": "hans@acme-fasteners.de",
            "phone": "+49 30 123456",
            "website": "https://www.acme-fasteners.de",
            "country": "Germany",
            "industry": "fasteners",
            "source_url": "https://example.com/detail/123",
        }
        lead = raw_to_lead(raw, "industry_site")
        assert lead is not None
        assert lead.company_name == "ACME Fasteners GmbH"
        assert lead.contact_name == "Hans Mueller"
        assert lead.email == "hans@acme-fasteners.de"
        assert lead.source == "industry_site"
        assert lead.status == "new"

    def test_infers_country_from_website(self):
        raw = {
            "company_name": "BoltMaster",
            "website": "https://boltmaster.de",
        }
        lead = raw_to_lead(raw, "industry_site")
        assert lead is not None
        assert lead.country == "Germany"

    def test_strips_junk_email(self):
        raw = {
            "company_name": "Test Corp",
            "email": "test@example.com",
        }
        lead = raw_to_lead(raw, "industry_site")
        assert lead is not None
        assert lead.email is None  # junk email stripped

    def test_skips_no_company_no_email(self):
        raw = {"contact_name": "Someone", "phone": "123"}
        lead = raw_to_lead(raw, "industry_site")
        assert lead is None

    def test_source_detail_json(self):
        raw = {
            "company_name": "Test Co",
            "source_detail": {"keyword": "fastener", "page": 3},
        }
        lead = raw_to_lead(raw, "xunkexing")
        assert lead is not None
        detail = json.loads(lead.source_detail)
        assert detail["keyword"] == "fastener"

    def test_default_industry(self):
        raw = {"company_name": "Some Company"}
        lead = raw_to_lead(raw, "industry_site")
        assert lead is not None
        assert lead.industry == "fasteners"


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline: ingest_leads
# ═══════════════════════════════════════════════════════════════════════════


class TestIngestLeads:
    def test_ingestion_creates_leads(self, db_conn):
        lead_mgr = LeadManager(db_conn)
        raw = [
            {"company_name": "Alpha GmbH", "email": "alpha@example.de"},
            {"company_name": "Beta Inc.", "email": "beta@betacorp.com"},
            {"company_name": "Gamma Ltd", "email": "gamma@example.co.uk"},
        ]
        result = ingest_leads(raw, "industry_site", lead_mgr)
        assert result["total_raw"] == 3
        assert result["created"] == 3
        assert result["duplicates_skipped"] == 0

        # Verify in DB
        leads = lead_mgr.list_all()
        assert len(leads) == 3

    def test_skips_invalid_leads(self, db_conn):
        lead_mgr = LeadManager(db_conn)
        raw = [
            {"company_name": "Valid Co", "email": "valid@validcorp.com"},
            {"company_name": None, "email": None},  # invalid
            {"company_name": "Another Valid", "email": "another@validcorp.com"},
        ]
        result = ingest_leads(raw, "industry_site", lead_mgr)
        assert result["total_raw"] == 3
        assert result["skipped_invalid"] == 1
        assert result["created"] == 2

    def test_in_batch_dedup(self, db_conn):
        lead_mgr = LeadManager(db_conn)
        raw = [
            {"company_name": "DupCo", "email": "dup@duptest.com"},
            {"company_name": "DupCo Again", "email": "dup@duptest.com"},  # same email
        ]
        result = ingest_leads(raw, "industry_site", lead_mgr)
        assert result["created"] == 1
        assert result["skipped_invalid"] == 1  # second is in-batch dup

    def test_db_level_dedup(self, db_conn):
        lead_mgr = LeadManager(db_conn)
        # First batch
        raw1 = [{"company_name": "Original", "email": "orig@deduptest.com"}]
        result1 = ingest_leads(raw1, "industry_site", lead_mgr)
        assert result1["created"] == 1

        # Second batch with same email
        raw2 = [{"company_name": "Duplicate", "email": "orig@deduptest.com"}]
        result2 = ingest_leads(raw2, "industry_site", lead_mgr)
        assert result2["duplicates_skipped"] == 1  # LeadManager dedup
        assert result2["created"] == 0

    def test_dry_run_no_write(self, db_conn):
        lead_mgr = LeadManager(db_conn)
        raw = [
            {"company_name": "DryCo", "email": "dry@drycorp.com"},
        ]
        result = ingest_leads(raw, "industry_site", lead_mgr, dry_run=True)
        assert result["dry_run"] is True
        assert result["created"] == 0
        assert result["valid"] == 1

        # Verify NOT written to DB
        leads = lead_mgr.list_all()
        assert len(leads) == 0

    def test_skips_no_email_leads(self, db_conn):
        """2026-08-08 policy: no-email leads must not be ingested."""
        import config as cfg
        lead_mgr = LeadManager(db_conn)
        raw = [
            {"company_name": "With Email", "email": "ok@okcorp.com"},
            {"company_name": "No Email Corp"},            # no email → skipped
            {"company_name": "Phone Only", "phone": "+49 123"},  # no email → skipped
        ]
        try:
            cfg.COLLECTION_REQUIRE_EMAIL = True
            result = ingest_leads(raw, "industry_site", lead_mgr)
            assert result["total_raw"] == 3
            assert result["created"] == 1
            assert result["skipped_invalid"] == 2
            assert lead_mgr.list_all()[0].email == "ok@okcorp.com"
        finally:
            cfg.COLLECTION_REQUIRE_EMAIL = os.getenv(
                "FTA_REQUIRE_EMAIL", "true"
            ).lower() == "true"

    def test_no_email_leads_allowed_when_flag_off(self, db_conn):
        """With the flag off, no-email leads are ingested (WhatsApp/phone use)."""
        import config as cfg
        lead_mgr = LeadManager(db_conn)
        raw = [
            {"company_name": "Phone Only", "phone": "+49 123"},
        ]
        try:
            cfg.COLLECTION_REQUIRE_EMAIL = False
            result = ingest_leads(raw, "industry_site", lead_mgr)
            assert result["created"] == 1
        finally:
            cfg.COLLECTION_REQUIRE_EMAIL = os.getenv(
                "FTA_REQUIRE_EMAIL", "true"
            ).lower() == "true"


# ═══════════════════════════════════════════════════════════════════════════
# BaseCollector
# ═══════════════════════════════════════════════════════════════════════════


class StubCollector(BaseCollector):
    """Minimal collector for testing the base class."""
    source_name = "test_stub"

    async def collect(self) -> list[dict]:
        return [
            {"company_name": "Stub Corp", "email": "stub@example.com"},
        ]


class TestBaseCollector:
    @pytest.mark.asyncio
    async def test_collect_returns_list(self):
        async with StubCollector() as collector:
            results = await collector.collect()
            assert isinstance(results, list)
            assert len(results) == 1
            assert results[0]["company_name"] == "Stub Corp"

    @pytest.mark.asyncio
    async def test_rate_limiting_waits(self):
        collector = StubCollector(delay=0.1)
        t0 = asyncio.get_event_loop().time()
        # Make two calls — second should be delayed
        await collector._wait_delay()
        await collector._wait_delay()
        elapsed = asyncio.get_event_loop().time() - t0
        # At least 0.1s delay between calls
        assert elapsed >= 0.1
        await collector.close()

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        async with StubCollector() as collector:
            assert collector.source_name == "test_stub"
        # After exit, client should be closed
        assert collector._client is None

    @pytest.mark.asyncio
    async def test_shared_client(self):
        """If a client is provided, collector should not close it."""
        import httpx
        shared = httpx.AsyncClient()
        collector = StubCollector(client=shared)
        assert collector._own_client is False
        await collector.close()
        assert not shared.is_closed  # shared client stays open
        await shared.aclose()


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


class TestCLI:
    def test_parser_run_source(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--source=industry"])
        assert args.command == "run"
        assert args.source == "industry"
        assert args.dry_run is False

    def test_parser_run_dry(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--source=industry", "--dry-run"])
        assert args.dry_run is True

    def test_parser_run_all(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--all"])
        assert args.all is True

    def test_parser_list_sources(self):
        parser = build_parser()
        args = parser.parse_args(["list-sources"])
        assert args.command == "list-sources"

    def test_parser_no_args_shows_help(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_source_registry_has_industry(self):
        settings = type("Settings", (), {
            "COLLECTION_INDUSTRY_ENABLED": True,
            "COLLECTION_XUNKEXING_ENABLED": False,
            "FB_COLLECTION_ENABLED": False,
            "COLLECTION_LINKEDIN_ENABLED": False,
        })()
        assert "industry" in build_source_registry(settings)

    def test_source_registry_pauses_xunkexing_by_default(self):
        assert "xunkexing" not in SOURCE_REGISTRY


# ═══════════════════════════════════════════════════════════════════════════
# Industry Collector (with mock HTTP)
# ═══════════════════════════════════════════════════════════════════════════


class TestIndustryCollector:
    def test_load_config_missing_file(self):
        from collector.sources.industry import IndustryCollector
        collector = IndustryCollector(
            sites_config=Path("/nonexistent/path.json")
        )
        sites = collector._load_config()
        assert sites == []

    def test_source_name(self):
        from collector.sources.industry import IndustryCollector
        collector = IndustryCollector()
        assert collector.source_name == "industry_site"

    @pytest.mark.asyncio
    async def test_collect_no_enabled_sites(self):
        from collector.sources.industry import IndustryCollector
        collector = IndustryCollector(
            sites_config=Path("/nonexistent/path.json")
        )
        results = await collector.collect()
        assert results == []
        await collector.close()

    def test_field_extraction(self):
        """Test that _parse_fields extracts text from HTML correctly."""
        from collector.sources.industry import IndustryCollector
        from bs4 import BeautifulSoup
        collector = IndustryCollector()

        html = """
        <html><body>
            <h1>ACME Fasteners GmbH</h1>
            <div class="phone">+49 30 123456</div>
            <a class="website" href="https://acme.de">Website</a>
            <div class="country">Germany</div>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        cfg = {
            "fields": {
                "company_name": "h1",
                "phone": ".phone",
                "website": "a.website",
                "country": ".country",
            }
        }
        result = collector._parse_fields(cfg, soup, "https://example.com/test")
        assert result["company_name"] == "ACME Fasteners GmbH"
        assert result["phone"] == "+49 30 123456"
        assert result["country"] == "Germany"

    def test_field_extraction_mailto(self):
        """Test that email is extracted from mailto links."""
        from collector.sources.industry import IndustryCollector
        from bs4 import BeautifulSoup
        collector = IndustryCollector()

        html = """
        <html><body>
            <h1>Test Co</h1>
            <a href="mailto:info@testco.com">Contact</a>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        cfg = {"fields": {"company_name": "h1"}}
        result = collector._parse_fields(cfg, soup, "https://example.com")
        assert result["email"] == "info@testco.com"


# ═══════════════════════════════════════════════════════════════════════════
# XunKeXing Collector
# ═══════════════════════════════════════════════════════════════════════════


class TestXunKeXingCollector:
    def test_source_name(self):
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(headless=True)
        assert collector.source_name == "xunkexing"

    def test_default_country_keys(self):
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(
            config_path=Path("/nonexistent/config.json"),
            headless=True,
        )
        assert len(collector.country_keys) > 0
        assert "united_states" in collector.country_keys

    def test_load_config_empty_when_missing(self):
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(
            config_path=Path("/nonexistent/config.json"),
            headless=True,
        )
        assert collector._cfg == {}

    def test_collector_async_context_manager(self):
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(headless=True)
        assert hasattr(collector, "__aenter__")
        assert hasattr(collector, "__aexit__")

    def test_api_lead_mapping(self):
        """Test _map_api_to_lead transforms API data correctly."""
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(headless=True)
        api_item = {
            "id": 123,
            "company_name": "ACME Fasteners GmbH",
            "email": "info@acme-fasteners.de",
            "telephone": "+49 30 123456",
            "country_area": "Germany (德国)",
            "category_name": "Industrial Supplies",
            "address": "123 Industrial St, Berlin",
            "website": "https://acme-fasteners.de",
            "fax": "+49 30 123457",
        }
        lead = collector._map_api_to_lead(api_item, "germany")
        assert lead is not None
        assert lead["company_name"] == "ACME Fasteners GmbH"
        assert lead["email"] == "info@acme-fasteners.de"
        assert lead["phone"] == "+49 30 123456"
        assert lead["country"] == "Germany"
        assert lead["industry"] == "Industrial Supplies"

    def test_api_lead_mapping_skips_empty_company(self):
        """Test that records without company_name are skipped."""
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(headless=True)
        api_item = {"id": 1, "company_name": "", "email": "test@test.com"}
        lead = collector._map_api_to_lead(api_item, "us")
        assert lead is None

    def test_normalize_country(self):
        from collector.sources.xunkexing import XunKeXingCollector
        assert XunKeXingCollector._normalize_country("United States (美国)") == "United States"
        assert XunKeXingCollector._normalize_country("Germany (德国)") == "Germany"
        assert XunKeXingCollector._normalize_country("") is None
        assert XunKeXingCollector._normalize_country(None) is None

    # ── Pagination (2026-08-08 fix: API never returns `total`, deep-page to empty) ──

    def test_pagination_pages_to_empty_without_total(self, monkeypatch):
        """Regression: old code gated on `total` (always 0 → stopped at page 1).

        New code must deep-page until an EMPTY page even when the API never
        returns a `total` field.
        """
        from collector.sources import xunkexing as xkx_mod
        from collector.sources.xunkexing import XunKeXingCollector

        collector = XunKeXingCollector(headless=True)
        collector.max_per_country = 500
        calls = {"n": 0}

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def raise_for_status(self):
                pass
            def json(self):
                return self._payload

        # 50 + 30 items, then an empty page = end-of-data marker. No `total`.
        pages = [
            {"code": 1, "data": [{"id": i, "company_name": f"Co{i}"} for i in range(50)]},
            {"code": 1, "data": [{"id": i + 100, "company_name": f"Co{i}_p2"} for i in range(30)]},
            {"code": 1, "data": []},
        ]

        async def seq_retry(request, **kwargs):
            idx = calls["n"]
            calls["n"] += 1
            return FakeResp(pages[idx])

        monkeypatch.setattr(xkx_mod, "async_retry", seq_retry)

        leads = asyncio.run(collector._fetch_country_leads("germany", ""))
        assert calls["n"] == 3      # p1, p2, p3(empty)
        assert len(leads) == 80     # 50 + 30, stopped at the empty page

    def test_pagination_respects_max_per_country(self, monkeypatch):
        """When the source never empties, pagination caps at max_per_country."""
        from collector.sources import xunkexing as xkx_mod
        from collector.sources.xunkexing import XunKeXingCollector

        collector = XunKeXingCollector(headless=True)
        collector.max_per_country = 100
        calls = {"n": 0}

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def raise_for_status(self):
                pass
            def json(self):
                return self._payload

        full_page = {"code": 1, "data": [
            {"id": i, "company_name": f"Co{i}"} for i in range(50)
        ]}

        async def seq_retry(request, **kwargs):
            idx = calls["n"]
            calls["n"] += 1
            return FakeResp(full_page)

        monkeypatch.setattr(xkx_mod, "async_retry", seq_retry)

        leads = asyncio.run(collector._fetch_country_leads("germany", ""))
        assert len(leads) == 100        # capped at max_per_country
        assert calls["n"] == 2          # p1(50), p2(100) — loop exits before p3

    def test_pagination_safety_page_cap(self, monkeypatch):
        """A never-ending source is stopped by the hard page-safety cap."""
        import config as cfg
        from collector.sources import xunkexing as xkx_mod
        from collector.sources.xunkexing import XunKeXingCollector

        collector = XunKeXingCollector(headless=True)
        collector.max_per_country = 10_000
        monkeypatch.setattr(cfg, "COLLECTION_MAX_PAGES_PER_QUERY", 3)
        calls = {"n": 0}

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload
            def raise_for_status(self):
                pass
            def json(self):
                return self._payload

        full_page = {"code": 1, "data": [
            {"id": i, "company_name": f"Co{i}"} for i in range(50)
        ]}

        async def seq_retry(request, **kwargs):
            idx = calls["n"]
            calls["n"] += 1
            return FakeResp(full_page)

        monkeypatch.setattr(xkx_mod, "async_retry", seq_retry)

        leads = asyncio.run(collector._fetch_country_leads("germany", ""))
        assert calls["n"] == 3          # p1, p2, p3 then page=4 > cap → break
        assert len(leads) == 150        # 3 pages × 50


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler
# ═══════════════════════════════════════════════════════════════════════════


class TestScheduler:
    def test_get_scheduler_status_stopped(self):
        from collector.scheduler import get_scheduler_status
        status = get_scheduler_status()
        # Scheduler may or may not be running (if previous test started it)
        assert "running" in status
        assert "sources_registered" in status

    def test_task_logging(self, db_conn):
        """Test that task logging writes to scraping_tasks table."""
        from collector.scheduler import _log_task_start, _log_task_complete

        # Override DB path to use the test in-memory DB (tricky with config)
        # Instead, test the functions directly if we mock config.DB_PATH
        # For now, test that functions exist and are callable
        task_id = _log_task_start("test_source")
        assert task_id is not None
        assert task_id > 0

        _log_task_complete(task_id, leads_found=10, leads_new=5)
        # Task complete should not crash

    def test_task_logging_records_pid(self):
        """_log_task_start must stamp the owner PID so shutdown cleanup can
        find and fail only this process's in-flight crawl tasks."""
        import config
        from collector.scheduler import _log_task_start

        task_id = _log_task_start("test_source")
        conn = sqlite3.connect(str(config.DB_PATH))
        try:
            row = conn.execute(
                "SELECT pid, status FROM scraping_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[1] == "running"
        assert row[0] == os.getpid()

    def test_mark_own_running_tasks_failed_pid_scoped(self):
        """Shutdown cleanup must fail ONLY tasks owned by this process, never
        a concurrent manual CLI collection (different PID, own task row)."""
        import config
        from crm.db import build_database
        from collector.scheduler import _mark_own_running_tasks_failed

        db_path = config.DB_PATH
        build_database(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO scraping_tasks (source, status, pid) VALUES "
            "('xunkexing', 'running', ?)", (os.getpid(),),
        )
        conn.execute(
            "INSERT INTO scraping_tasks (source, status, pid) VALUES "
            "('xunkexing', 'running', ?)", (os.getpid() + 1,),
        )
        conn.commit()
        conn.close()

        n = _mark_own_running_tasks_failed("test shutdown")
        assert n == 1  # only our own task

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT status FROM scraping_tasks ORDER BY id"
        ).fetchall()
        conn.close()
        assert [r[0] for r in rows] == ["failed", "running"]

    def test_migration_adds_scraping_tasks_pid(self, tmp_path):
        """A pre-migration DB (no pid column) gains it via _migrate_schema."""
        from crm.db import DB_SCHEMA, _migrate_schema

        # Simulate the pre-migration schema by stripping the pid column.
        schema_no_pid = DB_SCHEMA.replace("    pid INTEGER,\n", "")
        assert "pid INTEGER" not in schema_no_pid
        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(schema_no_pid)
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(db_path))
        try:
            _migrate_schema(conn)
            conn.commit()
        finally:
            conn.close()

        conn = sqlite3.connect(str(db_path))
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info('scraping_tasks')")}
        conn.close()
        assert "pid" in cols

    def test_get_recent_tasks_no_db(self):
        """Should return empty list when DB doesn't exist."""
        from collector.scheduler import get_recent_tasks
        # Point to a non-existent DB path
        with patch("collector.scheduler.config.DB_PATH", Path("/nonexistent/test.db")):
            tasks = get_recent_tasks()
            assert tasks == []


# ═══════════════════════════════════════════════════════════════════════════
# Rate Limiter
# ═══════════════════════════════════════════════════════════════════════════


class TestRateLimiter:
    def test_initial_state(self):
        from collector.rate_limiter import AdaptiveRateLimiter, RateLimiterConfig
        cfg = RateLimiterConfig(
            requests_per_minute=30, min_delay=1.0,
            max_consecutive_errors=5,
        )
        rl = AdaptiveRateLimiter(cfg, source_name="test")
        assert rl.current_delay == 1.0
        assert rl.is_circuit_open is False
        stats = rl.get_stats()
        assert stats["total_requests"] == 0
        assert stats["total_errors"] == 0
        assert stats["error_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_acquire_returns_quickly_with_unlimited_tokens(self):
        from collector.rate_limiter import AdaptiveRateLimiter, RateLimiterConfig
        cfg = RateLimiterConfig(
            requests_per_minute=0, min_delay=0.01, jitter_factor=0,
        )
        rl = AdaptiveRateLimiter(cfg, source_name="test")
        t0 = asyncio.get_event_loop().time()
        await rl.acquire()
        elapsed = asyncio.get_event_loop().time() - t0
        # Should return almost immediately
        assert elapsed < 0.5

    @pytest.mark.asyncio
    async def test_adaptive_backoff_on_errors(self):
        from collector.rate_limiter import AdaptiveRateLimiter, RateLimiterConfig
        cfg = RateLimiterConfig(
            requests_per_minute=0,  # unlimited for this test
            min_delay=1.0,
            backoff_multiplier=2.0,
            jitter_factor=0,  # disable jitter for deterministic test
        )
        rl = AdaptiveRateLimiter(cfg, source_name="test")
        assert rl.current_delay == 1.0

        rl.report_failure(503)
        assert rl.current_delay == 2.0  # 1.0 * 2.0

        rl.report_failure(503)
        assert rl.current_delay == 4.0  # 2.0 * 2.0

    @pytest.mark.asyncio
    async def test_gradual_recovery_after_successes(self):
        from collector.rate_limiter import AdaptiveRateLimiter, RateLimiterConfig
        cfg = RateLimiterConfig(
            requests_per_minute=0,
            min_delay=1.0,
            backoff_multiplier=2.0,
            recovery_divider=2.0,
            successes_for_recovery=3,
            jitter_factor=0,
        )
        rl = AdaptiveRateLimiter(cfg, source_name="test")

        # Push delay up
        rl.report_failure(503)
        assert rl.current_delay == 2.0

        # 3 successes should halve it
        rl.report_success()
        rl.report_success()
        rl.report_success()
        assert rl.current_delay == 1.0  # back to min

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_max_errors(self):
        from collector.rate_limiter import AdaptiveRateLimiter, RateLimiterConfig
        from collector.rate_limiter import CircuitBreakerOpen
        cfg = RateLimiterConfig(
            requests_per_minute=0,
            min_delay=0.01,
            max_consecutive_errors=3,
            circuit_cooldown_seconds=60.0,
            jitter_factor=0,
        )
        rl = AdaptiveRateLimiter(cfg, source_name="test")

        rl.report_failure()
        rl.report_failure()
        # 3rd failure should open circuit
        rl.report_failure()
        assert rl.is_circuit_open is True

        # Subsequent acquire should raise
        with pytest.raises(CircuitBreakerOpen):
            await rl.acquire()

    @pytest.mark.asyncio
    async def test_respects_retry_after_on_429(self):
        from collector.rate_limiter import AdaptiveRateLimiter, RateLimiterConfig
        cfg = RateLimiterConfig(
            requests_per_minute=0, min_delay=1.0, max_delay=30.0,
            jitter_factor=0,
        )
        rl = AdaptiveRateLimiter(cfg, source_name="test")

        rl.report_rate_limited(retry_after=10.0)
        assert rl.current_delay == 10.0  # uses server's retry_after

    @pytest.mark.asyncio
    async def test_reset_circuit(self):
        from collector.rate_limiter import AdaptiveRateLimiter, RateLimiterConfig
        cfg = RateLimiterConfig(
            requests_per_minute=0,
            max_consecutive_errors=2,
            circuit_cooldown_seconds=60.0,
            jitter_factor=0,
        )
        rl = AdaptiveRateLimiter(cfg, source_name="test")

        rl.report_failure()
        rl.report_failure()
        assert rl.is_circuit_open is True

        rl.reset_circuit()
        assert rl.is_circuit_open is False
        assert rl.current_delay == cfg.min_delay

    @pytest.mark.asyncio
    async def test_stats_reflect_state(self):
        from collector.rate_limiter import AdaptiveRateLimiter, RateLimiterConfig
        cfg = RateLimiterConfig(
            requests_per_minute=30, min_delay=0.01, jitter_factor=0,
        )
        rl = AdaptiveRateLimiter(cfg, source_name="test")
        await rl.acquire()
        rl.report_success()
        rl.report_failure(500)

        stats = rl.get_stats()
        assert stats["total_requests"] == 1
        assert stats["total_errors"] == 1
        assert stats["source"] == "test"


# ═══════════════════════════════════════════════════════════════════════════
# Retry Logic
# ═══════════════════════════════════════════════════════════════════════════


class TestRetryConfig:
    def test_default_values(self):
        from collector.retry import RetryConfig
        cfg = RetryConfig()
        assert cfg.max_retries == 3
        assert cfg.base_delay == 2.0
        assert 429 in cfg.retryable_statuses
        assert 401 in cfg.auth_error_statuses

    def test_custom_values(self):
        from collector.retry import RetryConfig
        cfg = RetryConfig(max_retries=5, base_delay=1.0)
        assert cfg.max_retries == 5
        assert cfg.base_delay == 1.0


class TestResponseValidation:
    def test_detect_captcha_cloudflare(self):
        from collector.retry import detect_captcha_or_block
        html = "<html><body>cf-browser-verify</body></html>"
        result = detect_captcha_or_block(html, "text/html")
        assert result is not None
        assert "Cloudflare" in result

    def test_detect_access_denied(self):
        from collector.retry import detect_captcha_or_block
        html = "<html><body><h1>Access Denied</h1></body></html>"
        result = detect_captcha_or_block(html, "text/html")
        assert result is not None
        assert "access denied" in result

    def test_detect_cn_captcha(self):
        from collector.retry import detect_captcha_or_block
        html = "<html><body>请输入验证码</body></html>"
        result = detect_captcha_or_block(html, "text/html")
        assert result is not None
        assert "captcha (CN)" in result

    def test_no_captcha_in_normal_page(self):
        from collector.retry import detect_captcha_or_block
        html = "<html><body><h1>Welcome to our B2B platform</h1></body></html>"
        result = detect_captcha_or_block(html, "text/html")
        assert result is None

    def test_html_body_with_json_content_type(self):
        """If content-type is JSON but body is HTML, it's suspicious."""
        from collector.retry import detect_captcha_or_block
        html = "<!DOCTYPE html><html>...</html>"
        result = detect_captcha_or_block(html, "application/json")
        assert result is not None
        assert "HTML" in result

    def test_validate_api_response_auth_code(self):
        from collector.retry import validate_api_response
        data = {"code": -1, "msg": "token已过期"}
        result = validate_api_response(data, "test")
        assert result is not None
        assert "token" in result.lower() or "过期" in result

    def test_validate_api_response_clean(self):
        from collector.retry import validate_api_response
        data = {"code": 1, "msg": "success", "data": []}
        result = validate_api_response(data, "test")
        assert result is None

    def test_validate_api_response_login_required(self):
        from collector.retry import validate_api_response
        data = {"code": 0, "msg": "请重新登录"}
        result = validate_api_response(data, "test")
        assert result is not None


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_successful_first_attempt(self):
        from collector.retry import async_retry, RetryConfig
        call_count = 0

        async def succeed():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await async_retry(succeed, config=RetryConfig(max_retries=2), context="test")
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_raises_auth_error_immediately(self):
        from collector.retry import async_retry, RetryConfig, AuthErrorDetected
        import httpx
        call_count = 0

        async def auth_fail():
            nonlocal call_count
            call_count += 1
            resp = httpx.Response(401)
            raise httpx.HTTPStatusError("Unauthorized", request=MagicMock(), response=resp)

        with pytest.raises(AuthErrorDetected):
            await async_retry(auth_fail, config=RetryConfig(max_retries=3), context="test")
        # Should NOT retry auth errors — only 1 call
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_503(self):
        from collector.retry import async_retry, RetryConfig, MaxRetriesExceeded
        import httpx
        call_count = 0

        async def always_503():
            nonlocal call_count
            call_count += 1
            resp = httpx.Response(503)
            raise httpx.HTTPStatusError("Service Unavailable", request=MagicMock(), response=resp)

        with pytest.raises(MaxRetriesExceeded):
            await async_retry(
                always_503,
                config=RetryConfig(max_retries=2, base_delay=0.01, jitter=False),
                context="test",
            )
        # 1 initial + 2 retries = 3 total
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self):
        from collector.retry import async_retry, RetryConfig, MaxRetriesExceeded
        call_count = 0

        async def connection_fail():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("Connection refused")

        with pytest.raises(MaxRetriesExceeded):
            await async_retry(
                connection_fail,
                config=RetryConfig(max_retries=2, base_delay=0.01, jitter=False),
                context="test",
            )
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_transient_failure(self):
        from collector.retry import async_retry, RetryConfig
        import httpx
        call_count = 0

        async def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                resp = httpx.Response(503)
                raise httpx.HTTPStatusError("fail", request=MagicMock(), response=resp)
            return "recovered"

        result = await async_retry(
            fail_then_succeed,
            config=RetryConfig(max_retries=3, base_delay=0.01, jitter=False),
            context="test",
        )
        assert result == "recovered"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_400(self):
        from collector.retry import async_retry, RetryConfig
        import httpx
        call_count = 0

        async def bad_request():
            nonlocal call_count
            call_count += 1
            resp = httpx.Response(400)
            raise httpx.HTTPStatusError("Bad Request", request=MagicMock(), response=resp)

        with pytest.raises(httpx.HTTPStatusError):
            await async_retry(bad_request, config=RetryConfig(max_retries=3), context="test")
        assert call_count == 1  # 400 is not retryable

    @pytest.mark.asyncio
    async def test_invalidates_token_on_auth_error(self):
        """XunKeXing collector should invalidate cached token on auth error."""
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(headless=True)
        # _invalidate_token should exist as a method
        assert hasattr(collector, "_invalidate_token")


# ═══════════════════════════════════════════════════════════════════════════
# Risk Control: Auth & Block Detection (XunKeXing-specific)
# ═══════════════════════════════════════════════════════════════════════════


class TestXunKeXingRiskControl:
    def test_auth_error_codes_defined(self):
        from collector.sources.xunkexing import XKX_AUTH_ERROR_CODES
        assert -1 in XKX_AUTH_ERROR_CODES
        assert 401 in XKX_AUTH_ERROR_CODES

    def test_risk_keywords_include_login(self):
        from collector.sources.xunkexing import XKX_RISK_KEYWORDS
        assert "token" in XKX_RISK_KEYWORDS
        assert "登录" in XKX_RISK_KEYWORDS
        assert "过期" in XKX_RISK_KEYWORDS

    def test_collector_has_rate_limiter(self):
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(headless=True)
        assert hasattr(collector, "_rate_limiter")
        from collector.rate_limiter import AdaptiveRateLimiter
        assert isinstance(collector._rate_limiter, AdaptiveRateLimiter)

    def test_collector_has_retry_config(self):
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(headless=True)
        assert hasattr(collector, "_retry_config")
        from collector.retry import RetryConfig
        assert isinstance(collector._retry_config, RetryConfig)

    def test_rate_limiter_uses_config_values(self):
        """Rate limiter should pick up settings from config module."""
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(
            headless=True, requests_per_minute=15, min_delay=2.0,
        )
        stats = collector._rate_limiter.get_stats()
        assert stats["max_tokens"] == 15
        assert collector._rate_limiter.current_delay == 2.0

    def test_country_results_initialized(self):
        """Collector should have _country_results dict for scheduler integration."""
        from collector.sources.xunkexing import XunKeXingCollector
        collector = XunKeXingCollector(headless=True)
        assert hasattr(collector, "_country_results")
        assert isinstance(collector._country_results, dict)


# ═══════════════════════════════════════════════════════════════════════════
# Segment Manager
# ═══════════════════════════════════════════════════════════════════════════


class TestSegmentManager:
    def test_select_all_when_smaller_than_segment(self, db_conn):
        """If we have fewer countries than segment_size, pick all."""
        from collector.segments import SegmentManager
        mgr = SegmentManager(db_conn)
        countries = ["us", "de", "uk"]
        selected = mgr.select_today_countries(countries, segment_size=10)
        assert len(selected) == 3
        assert set(selected) == set(countries)

    def test_select_exact_segment_size(self, db_conn):
        """Select exactly segment_size countries."""
        from collector.segments import SegmentManager
        mgr = SegmentManager(db_conn)
        countries = ["us", "de", "uk", "fr", "it", "es", "nl", "pl"]
        selected = mgr.select_today_countries(countries, segment_size=4)
        assert len(selected) == 4
        # All selected should be from the valid set
        assert set(selected).issubset(set(countries))

    def test_prioritizes_never_collected(self, db_conn):
        """Never-collected countries should be selected first."""
        from collector.segments import SegmentManager
        mgr = SegmentManager(db_conn)
        countries = ["a", "b", "c", "d", "e", "f"]

        # Mark some as already collected recently
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db_conn.execute(
            "INSERT INTO collection_segments (country_key, last_collected_at) VALUES (?, ?)",
            ("a", now),
        )
        db_conn.execute(
            "INSERT INTO collection_segments (country_key, last_collected_at) VALUES (?, ?)",
            ("b", now),
        )
        db_conn.commit()

        selected = mgr.select_today_countries(countries, segment_size=2)
        # "c", "d", "e", "f" never collected — should be prioritized
        for s in selected:
            assert s not in ("a", "b")  # the recently-collected ones

    def test_record_collection_updates_state(self, db_conn):
        from collector.segments import SegmentManager
        mgr = SegmentManager(db_conn)

        mgr.record_collection("germany", leads_found=10, had_error=False)
        status = mgr.get_rotation_status()
        germany = next(s for s in status if s["country_key"] == "germany")
        assert germany["collection_count"] == 1
        assert germany["last_leads_found"] == 10
        assert germany["consecutive_errors"] == 0
        assert germany["last_collected_at"] is not None

    def test_record_collection_error_increments_count(self, db_conn):
        from collector.segments import SegmentManager
        mgr = SegmentManager(db_conn)

        mgr.record_collection("france", leads_found=0, had_error=True)
        mgr.record_collection("france", leads_found=0, had_error=True)
        status = mgr.get_rotation_status()
        france = next(s for s in status if s["country_key"] == "france")
        assert france["consecutive_errors"] == 2

    def test_success_resets_error_count(self, db_conn):
        from collector.segments import SegmentManager
        mgr = SegmentManager(db_conn)

        mgr.record_collection("italy", leads_found=0, had_error=True)
        # Error doesn't increment collection_count — only success does
        mgr.record_collection("italy", leads_found=15, had_error=False)
        mgr.record_collection("italy", leads_found=10, had_error=False)
        status = mgr.get_rotation_status()
        italy = next(s for s in status if s["country_key"] == "italy")
        assert italy["consecutive_errors"] == 0
        assert italy["collection_count"] == 2  # only successful collections counted
        assert italy["last_leads_found"] == 10


# ═══════════════════════════════════════════════════════════════════════════
# Health Monitor
# ═══════════════════════════════════════════════════════════════════════════


class TestHealthMonitor:
    def test_initial_state(self):
        from collector.monitor import HealthMonitor
        m = HealthMonitor(source="test")
        assert m.total_requests == 0
        assert m.error_count == 0
        assert m.compute_health_score() == 100.0  # no requests = perfect
        assert m.success_rate == 1.0

    def test_records_requests(self):
        from collector.monitor import HealthMonitor
        m = HealthMonitor(source="test")
        m.record_request(latency_ms=100.0, status_code=200)
        m.record_request(latency_ms=250.0, status_code=200)
        m.record_request(latency_ms=0, status_code=500, error_type="http_500")
        assert m.total_requests == 3
        assert m.error_count == 1
        assert m.avg_latency_ms == 175.0  # (100 + 250) / 2

    def test_health_score_perfect(self):
        from collector.monitor import HealthMonitor
        m = HealthMonitor(source="test")
        m.start_run(countries_total=3)
        for _ in range(10):
            m.record_request(latency_ms=200.0, status_code=200)
        m.record_country_result("us", success=True, leads_found=5)
        m.record_country_result("de", success=True, leads_found=8)
        m.record_country_result("uk", success=True, leads_found=3)
        m.end_run(leads_found=16, leads_new=16)
        assert m.compute_health_score() > 90

    def test_health_score_drops_with_errors(self):
        from collector.monitor import HealthMonitor
        m = HealthMonitor(source="test")
        m.start_run(countries_total=3)
        # 50% error rate
        for _ in range(5):
            m.record_request(latency_ms=100.0, status_code=200)
        for _ in range(5):
            m.record_request(latency_ms=0, status_code=500, error_type="http_500")
        m.end_run(leads_found=5, leads_new=5)
        score = m.compute_health_score()
        assert score < 80  # Should be penalized for 50% error rate

    def test_health_score_drops_with_rate_limits(self):
        from collector.monitor import HealthMonitor
        m = HealthMonitor(source="test")
        m.start_run(countries_total=1)
        m.record_request(latency_ms=100.0, status_code=429, error_type="http_429")
        m.record_request(latency_ms=200.0, status_code=200)
        m.end_run(leads_found=1, leads_new=1)
        score = m.compute_health_score()
        assert score < 95  # 1 rate limit = -10

    def test_saves_to_db(self, db_conn):
        from collector.monitor import HealthMonitor
        m = HealthMonitor(source="test_source")
        m.start_run(countries_total=2)
        m.record_request(latency_ms=150.0, status_code=200)
        m.record_request(latency_ms=300.0, status_code=200)
        m.record_country_result("us", success=True, leads_found=5)
        m.record_country_result("de", success=True, leads_found=3)
        m.end_run(leads_found=8, leads_new=8)
        m.save_to_db(db_conn, task_id=None)

        # Verify saved
        rows = db_conn.execute(
            "SELECT * FROM collection_health WHERE source='test_source'"
        ).fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["total_requests"] == 2
        assert row["leads_found"] == 8
        assert row["health_score"] > 90

    def test_get_summary(self):
        from collector.monitor import HealthMonitor
        m = HealthMonitor(source="test")
        m.start_run(countries_total=2)
        m.record_request(latency_ms=123.0, status_code=200)
        m.end_run(leads_found=5, leads_new=5)
        summary = m.get_summary()
        assert summary["source"] == "test"
        assert summary["total_requests"] == 1
        assert "health_score" in summary
        assert "avg_latency_ms" in summary


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler: Time Randomization & Config
# ═══════════════════════════════════════════════════════════════════════════


class TestSchedulerRandomization:
    def test_pick_random_time_in_window(self):
        from collector.scheduler import _pick_random_time
        # Patch config to control the window
        import collector.scheduler as sched_mod
        orig_start = sched_mod.config.SCHEDULE_TIME_WINDOW_START
        orig_end = sched_mod.config.SCHEDULE_TIME_WINDOW_END
        try:
            sched_mod.config.SCHEDULE_TIME_WINDOW_START = 7
            sched_mod.config.SCHEDULE_TIME_WINDOW_END = 10
            for _ in range(20):
                hour, minute = _pick_random_time()
                assert 7 <= hour < 10
                assert 0 <= minute <= 59
                # Should avoid :00 and :30
                assert minute not in (0, 30)
        finally:
            sched_mod.config.SCHEDULE_TIME_WINDOW_START = orig_start
            sched_mod.config.SCHEDULE_TIME_WINDOW_END = orig_end

    def test_format_next_run(self):
        from collector.scheduler import _format_next_run
        result = _format_next_run(8, 15)
        assert "08:15" in result or "08:15:00" in result

    def test_scheduler_status_includes_new_fields(self):
        from collector.scheduler import get_scheduler_status
        status = get_scheduler_status()
        assert "running" in status
        assert "time_window" in status
        assert "segmentation" in status
        assert "latest_health" in status
        assert "next_run" in status

    def test_scheduler_status_segmentation_config(self):
        from collector.scheduler import get_scheduler_status
        status = get_scheduler_status()
        seg = status.get("segmentation", {})
        assert "segment_size" in seg


# ═══════════════════════════════════════════════════════════════════════════
# Facebook Collector
# ═══════════════════════════════════════════════════════════════════════════


class TestFacebookCollector:
    """Tests for Facebook collector (unit tests, no Playwright needed)."""

    def test_source_name(self):
        from collector.sources.facebook import FacebookCollector
        assert FacebookCollector.source_name == "facebook"

    def test_default_queries(self):
        from collector.sources.facebook import FB_SEARCH_QUERIES
        assert len(FB_SEARCH_QUERIES) >= 4
        assert any("fastener" in q for q in FB_SEARCH_QUERIES)
        assert any("五金" in q for q in FB_SEARCH_QUERIES)

    def test_custom_queries(self):
        from collector.sources.facebook import FacebookCollector
        c = FacebookCollector.__new__(FacebookCollector)
        c.queries = ["custom fasteners query"]
        assert len(c.queries) == 1
        assert c.queries[0] == "custom fasteners query"

    def test_auth_block_detection_login_title(self):
        from collector.sources.facebook import FacebookCollector
        c = FacebookCollector.__new__(FacebookCollector)
        html = "<html><head><title>Log in to Facebook</title></head><body>please log in to continue</body></html>"
        assert c._is_blocked_or_auth(html) is True

    def test_auth_block_detection_security_check(self):
        from collector.sources.facebook import FacebookCollector
        c = FacebookCollector.__new__(FacebookCollector)
        html = "<html><body>Unusual activity detected. Please confirm your identity. Verify your account.</body></html>"
        assert c._is_blocked_or_auth(html) is True

    def test_auth_block_detection_normal_page(self):
        from collector.sources.facebook import FacebookCollector
        c = FacebookCollector.__new__(FacebookCollector)
        html = "<html><body>fasteners group post about bolts and nuts for sale contact us</body></html>"
        assert c._is_blocked_or_auth(html) is False

    def test_clean_name_removes_artifacts(self):
        from collector.sources.facebook import FacebookCollector
        result = FacebookCollector._clean_name("ABC Fasteners | Facebook")
        assert result == "ABC Fasteners"

    def test_clean_name_short_rejected(self):
        from collector.sources.facebook import FacebookCollector
        result = FacebookCollector._clean_name("A")
        assert result is None

    def test_clean_name_none(self):
        from collector.sources.facebook import FacebookCollector
        assert FacebookCollector._clean_name(None) is None

    def test_find_whatsapp_valid(self):
        from collector.sources.facebook import FacebookCollector
        result = FacebookCollector._find_whatsapp(["+1-555-123-4567", "+447700900123"])
        assert result == "+15551234567"

    def test_find_whatsapp_short(self):
        from collector.sources.facebook import FacebookCollector
        result = FacebookCollector._find_whatsapp(["12345", "67890"])
        assert result is None

    def test_infer_country_from_text(self):
        from collector.sources.facebook import FacebookCollector
        result = FacebookCollector._infer_country(
            "Looking for bolt suppliers in Germany. Contact us!",
            "John Smith"
        )
        assert result == "Germany"

    def test_infer_country_none_found(self):
        from collector.sources.facebook import FacebookCollector
        result = FacebookCollector._infer_country(
            "Industrial fasteners for sale",
            "Trader123"
        )
        assert result is None

    def test_truncate_short_text(self):
        from collector.sources.facebook import FacebookCollector
        text = "Short post"
        assert FacebookCollector._truncate(text, 500) == text

    def test_truncate_long_text(self):
        from collector.sources.facebook import FacebookCollector
        text = "A" * 600
        result = FacebookCollector._truncate(text, 100)
        assert len(result) <= 103  # 100 + "..."
        assert result.endswith("...")

    def test_profile_dir_default(self):
        from collector.sources.facebook import FB_PROFILE_DIR
        from pathlib import Path
        import config
        assert FB_PROFILE_DIR == Path(config.DATA_DIR) / "fb_profile"

    def test_email_regex_extraction(self):
        from collector.sources.facebook import EMAIL_RE
        text = "Contact us at sales@fastener-co.com or info@bolts.de for pricing"
        matches = EMAIL_RE.findall(text)
        assert len(matches) == 2
        assert "sales@fastener-co.com" in matches
        assert "info@bolts.de" in matches
