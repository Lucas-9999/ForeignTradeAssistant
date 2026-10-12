"""Tests for segment matching, per-lead template selection, and template integrity."""

import json
import re
import sqlite3
from pathlib import Path

import pytest

from emailer.segmentation import SegmentMatcher
from emailer.templates import TemplateLoader, sync_templates_to_db


@pytest.fixture
def matcher():
    return SegmentMatcher()


# ---------------------------------------------------------------------------
# SegmentMatcher: industry mapping
# ---------------------------------------------------------------------------


class TestIndustryMapping:
    def test_known_industries(self, matcher):
        cases = {
            "Automotive & Vehicles": "automotive",
            "electric scooter parts": "automotive",
            "Machinery": "machinery",
            "ball screw": "machinery",
            "truck mounted crane": "machinery",
            "Building Materials": "construction",
            "Electronics & Electrical Equipment": "electronics",
            "Consumer Electronics": "electronics",
            "Lighting": "electronics",
            "Solar": "new_energy",
            "Medical Devices": "medical",
            "fasteners": "hardware_furniture",
            "Hardware": "hardware_furniture",
            "Hardware tool": "hardware_furniture",
        }
        for label, expected in cases.items():
            group, low = matcher.industry_group(label)
            assert group == expected, label
            assert low is False, label

    def test_low_relevance(self, matcher):
        for label in [
            "Food & Beverage",
            "Footwear",
            "Toys & Hobbies",
            "Kitchenware & Tableware",
            "Bags, Luggage & Leather Goods",
            "Ceramics & Porcelain Products",
            "metal jar",
            "Sealing stickers",
        ]:
            group, low = matcher.industry_group(label)
            assert group == "general", label
            assert low is True, label

    def test_unknown_and_empty(self, matcher):
        assert matcher.industry_group("") == ("general", False)
        assert matcher.industry_group(None) == ("general", False)
        assert matcher.industry_group("XYZ Unknown Co") == ("general", False)

    def test_priority_resolves_overlapping_keywords(self, matcher):
        # 'electric scooter motor' matches automotive ('scooter') AND machinery
        # ('motor'); priority order must resolve it to automotive.
        assert matcher.industry_group("electric scooter motor") == (
            "automotive",
            False,
        )
        # 'ball screw' matches machinery ('ball screw') AND hardware_furniture
        # ('screw'); machinery has higher priority.
        assert matcher.industry_group("ball screw") == ("machinery", False)

        # SQL predicate for the top-priority group stays a plain keyword match;
        # every lower group must exclude industries a higher group claims first.
        assert "NOT" not in matcher.group_predicate("automotive")
        assert "NOT" in matcher.group_predicate("machinery")
        assert "NOT" in matcher.group_predicate("hardware_furniture")

    def test_group_predicate_matches_python_semantics(self, matcher):
        """The SQL predicate must accept exactly the labels industry_group() assigns."""
        overlap_labels = {
            "electric scooter motor": "automotive",
            "electric scooter battery": "automotive",
            "ball screw": "machinery",
            "CNC machining center": "machinery",
        }
        import sqlite3

        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE t (industry TEXT)")
            for label, expected in overlap_labels.items():
                group, _low = matcher.industry_group(label)
                assert group == expected, label

                # The group's predicate must MATCH the label in SQL...
                conn.execute("DELETE FROM t")
                conn.execute("INSERT INTO t (industry) VALUES (?)", (label,))
                rows = conn.execute(
                    "SELECT COUNT(*) FROM t WHERE " + matcher.group_predicate(group)
                ).fetchone()
                assert rows[0] == 1, f"{label} should match {group}"

                # ...and a different group's predicate must NOT match it.
                for other in ("automotive", "machinery", "hardware_furniture"):
                    if other == group:
                        continue
                    rows = conn.execute(
                        "SELECT COUNT(*) FROM t WHERE " + matcher.group_predicate(other)
                    ).fetchone()
                    assert rows[0] == 0, (
                        f"{label} must NOT match {other} "
                        f"(sql={matcher.group_predicate(other)})"
                    )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# SegmentMatcher: country grouping + region variables
# ---------------------------------------------------------------------------


class TestCountryMapping:
    def test_groups(self, matcher):
        cases = {
            "United States": "north_america",
            "Canada": "north_america",
            "Germany": "europe",
            "France": "europe",
            "Russia": "europe",
            "Japan": "northeast_asia",
            "South Korea": "northeast_asia",
            "Australia": "oceania",
            "Singapore": "southeast_asia",
            "UAE": "middle_east",
            "India": "generic",
            "Brazil": "generic",
            "": "generic",
        }
        for country, expected in cases.items():
            assert matcher.country_group(country) == expected, country

    def test_region_localization(self, matcher):
        region = matcher.region("Germany")
        assert region["greeting"] == "Hallo"
        assert "Gr" in region["signoff"]
        assert region["country_group"] == "europe"

        region = matcher.region("Japan")
        assert region["greeting"] != "Hello"
        assert region["country_group"] == "northeast_asia"

        region = matcher.region("United States")
        assert region["greeting"] == "Hi"
        assert region["country_group"] == "north_america"

    def test_priority_case_and_predicates(self, matcher):
        assert "CASE" in matcher.priority_case()
        assert "automotive" in matcher.group_predicate("automotive")
        assert "industry IS NULL" in matcher.group_predicate("general")
        assert "instr" in matcher.low_relevance_predicate()


# ---------------------------------------------------------------------------
# TemplateLoader: per-lead selection + rotation
# ---------------------------------------------------------------------------


class TestPickForLead:
    def setup_method(self):
        self.loader = TemplateLoader()

    def test_industry_match(self):
        lead = {"industry": "Automotive & Vehicles"}
        template = self.loader.pick_for_lead(lead)
        assert template["industry_group"] == "automotive"

    def test_general_fallback(self):
        lead = {"industry": "Food & Beverage"}
        template = self.loader.pick_for_lead(lead)
        assert template["industry_group"] == "general"

    def test_unknown_industry_fallback(self):
        lead = {"industry": ""}
        template = self.loader.pick_for_lead(lead)
        assert template["industry_group"] == "general"

    def test_rotation_within_group(self):
        lead = {"industry": "Automotive & Vehicles"}
        used = set()
        for _ in range(4):
            t = self.loader.pick_for_lead(lead)
            used.add(t["id"])
            self.loader.mark_used(t["id"])
        assert len(used) == 3  # all automotive variants eventually used
        assert all("auto" in tid for tid in used)

    def test_region_variables_in_render(self):
        t = self.loader.pick_for_lead({"industry": "Automotive & Vehicles"})
        rendered = self.loader.render_email(
            t,
            company_name="ACME",
            contact_name="Hans",
            country="Germany",
            industry="Automotive & Vehicles",
        )
        assert "Hallo Hans" in rendered["body_html"]


# ---------------------------------------------------------------------------
# Template library integrity
# ---------------------------------------------------------------------------


class TestTemplateLibrary:
    def setup_method(self):
        self.loader = TemplateLoader()

    def test_count_and_required_fields(self):
        assert self.loader.template_count == 25
        for t in self.loader._templates:
            for key in ["id", "name", "category", "industry_group", "variant",
                        "subject", "body"]:
                assert key in t, f"{t.get('id')} missing {key}"

    def test_unique_ids_and_followups(self):
        ids = [t["id"] for t in self.loader._templates]
        assert len(ids) == len(set(ids))
        assert {"followup_01", "followup_02", "followup_03"} <= set(ids)

    def test_all_render_and_lengths(self):
        for t in self.loader._templates:
            rendered = self.loader.render_email(
                t,
                company_name="ACME Corp",
                contact_name="John Smith",
                country="Germany",
                industry="Machinery",
            )
            subject_words = len(rendered["subject"].split())
            assert subject_words <= 8, f"{t['id']} subject too long: {rendered['subject']}"
            text = re.sub(r"<[^>]+>", " ", rendered["body_html"])
            word_count = len(text.split())
            assert 50 <= word_count <= 125, f"{t['id']} has {word_count} words"

    def test_no_banned_claims(self):
        for t in self.loader._templates:
            text = (t["subject"] + " " + t["body"]).lower()
            assert "20-35%" not in text and "20–35%" not in text, t["id"]
            assert "moq" not in text, t["id"]

    def test_subjects_avoid_promotional_free_language(self):
        for t in self.loader._templates:
            assert "free" not in t["subject"].lower(), t["id"]

    def test_templates_are_explicitly_synthetic(self):
        for template in self.loader._templates:
            assert "synthetic preview" in template["body"].lower()
            assert "no message will be sent" in template["body"].lower()
            assert "fictional" in template["body"].lower()


# ---------------------------------------------------------------------------
# Batch sender: segment filtering + priority ordering (dry run)
# ---------------------------------------------------------------------------


@pytest.fixture
def batch_db(tmp_path, monkeypatch):
    """Build a temp DB and point config.DB_PATH at it."""
    import config
    from crm.db import build_database

    db_path = tmp_path / "batch_test.db"
    build_database(db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    return db_path


def _insert_lead(conn, company, industry, country, email, status="new"):
    conn.execute(
        """INSERT INTO leads (company_name, industry, country, email, status)
           VALUES (?, ?, ?, ?, ?)""",
        (company, industry, country, email, status),
    )


class TestBatchSegments:
    def test_generic_recipient_refusal_is_not_permanently_suppressed(
        self, batch_db, monkeypatch
    ):
        from emailer.batch import send_batch

        monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
        monkeypatch.setattr(
            "emailer.sender.send_email_sync",
            lambda **kwargs: (False, "Recipient refused: buyer@example.com", None),
        )
        conn = sqlite3.connect(str(batch_db))
        _insert_lead(
            conn,
            "Refused Co",
            "Automotive & Vehicles",
            "Germany",
            "buyer@example.com",
        )
        conn.commit()
        conn.close()

        result = send_batch(limit=1, dry_run=False)

        conn = sqlite3.connect(str(batch_db))
        lead_status = conn.execute("SELECT status FROM leads").fetchone()[0]
        suppression_count = conn.execute(
            "SELECT COUNT(*) FROM email_suppressions"
        ).fetchone()[0]
        conn.close()
        assert result["errors"] == 0
        assert result["skipped"] == 1
        assert lead_status == "new"
        assert suppression_count == 0

    def test_confirmed_hard_bounce_marks_lead_dead_and_suppresses(
        self, batch_db, monkeypatch
    ):
        from emailer.batch import send_batch

        monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
        monkeypatch.setattr(
            "emailer.sender.send_email_sync",
            lambda **kwargs: (False, "HARD_BOUNCE: 550 5.1.1 user unknown", None),
        )
        conn = sqlite3.connect(str(batch_db))
        _insert_lead(
            conn,
            "Hard Bounce Co",
            "Automotive & Vehicles",
            "Germany",
            "gone@example.com",
        )
        conn.commit()
        conn.close()

        result = send_batch(limit=1, dry_run=False)

        conn = sqlite3.connect(str(batch_db))
        lead_status = conn.execute("SELECT status FROM leads").fetchone()[0]
        suppression = conn.execute(
            "SELECT reason, source FROM email_suppressions"
        ).fetchone()
        conn.close()
        assert result["errors"] == 0
        assert result["skipped"] == 1
        assert lead_status == "new"
        assert suppression is None

    def test_industry_filter_and_priority(self, batch_db, monkeypatch):
        from emailer.batch import send_batch
        monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)

        conn = sqlite3.connect(str(batch_db))
        _insert_lead(conn, "Auto A", "Automotive & Vehicles", "Germany", "a@a.de")
        _insert_lead(conn, "Auto B", "Automotive & Vehicles", "USA", "b@b.com")
        _insert_lead(conn, "Food Co", "Food & Beverage", "USA", "f@f.com")
        conn.commit()
        conn.close()

        # Without filters, core industries (automotive) are picked before low-relevance.
        result = send_batch(limit=2, dry_run=True)
        companies = {d["company"] for d in result["details"]}
        assert companies == {"Auto A", "Auto B"}

        # Industry filter restricts to that group.
        result = send_batch(limit=5, dry_run=True, industry_group="general",
                            include_low_relevance=True)
        companies = [d["company"] for d in result["details"]]
        assert "Food Co" in companies

    def test_industry_filter_respects_priority(self, batch_db, monkeypatch):
        """Overlapping keywords must not leak leads into a lower-priority filter.

        'electric scooter motor' matches machinery's 'motor' but automotive's
        'scooter' wins by priority — so an --industry-group=machinery batch must
        NOT include it.
        """
        from emailer.batch import send_batch

        monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
        conn = sqlite3.connect(str(batch_db))
        _insert_lead(conn, "Scooter Motor Co", "electric scooter motor",
                     "Germany", "sm@de.de")
        _insert_lead(conn, "CNC Machine Co", "CNC machining center",
                     "USA", "cnc@us.com")
        conn.commit()
        conn.close()

        result = send_batch(limit=5, dry_run=True, industry_group="machinery")
        companies = [d["company"] for d in result["details"]]
        assert companies == ["CNC Machine Co"]

        result = send_batch(limit=5, dry_run=True, industry_group="automotive")
        companies = [d["company"] for d in result["details"]]
        assert companies == ["Scooter Motor Co"]

    def test_country_group_filter(self, batch_db, monkeypatch):
        from emailer.batch import send_batch

        monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
        conn = sqlite3.connect(str(batch_db))
        _insert_lead(conn, "DE Co", "Machinery", "Germany", "de@de.de")
        _insert_lead(conn, "US Co", "Machinery", "United States", "us@us.com")
        conn.commit()
        conn.close()

        result = send_batch(limit=5, dry_run=True, country_group="europe")
        companies = [d["company"] for d in result["details"]]
        assert companies == ["DE Co"]

    def test_dry_run_rotation_varies(self, batch_db, monkeypatch):
        from emailer.batch import send_batch

        monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
        conn = sqlite3.connect(str(batch_db))
        for i in range(4):
            _insert_lead(
                conn,
                f"Auto {i}",
                "Automotive & Vehicles",
                "Germany",
                f"auto{i}@de.de",
            )
        conn.commit()
        conn.close()

        result = send_batch(limit=4, dry_run=True)
        template_ids = [d.get("template_id") for d in result["details"]]
        assert len(template_ids) == 4
        assert len(set(template_ids)) > 1  # not all the same letter

    def test_batch_status_is_per_profile(self, batch_db, monkeypatch):
        """NetEase + Zoho batches keep separate live-status entries."""
        from emailer.batch import send_batch, get_batch_status

        monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
        conn = sqlite3.connect(str(batch_db))
        _insert_lead(conn, "XK Co", "Automotive & Vehicles", "Germany", "x@xk.de")
        conn.commit()
        conn.close()

        send_batch(limit=1, dry_run=True, smtp_profile="default")
        send_batch(limit=1, dry_run=True, smtp_profile="zoho")

        # Zoho's own status is tracked under its profile, not clobbered.
        # (dry-run counts toward `total`; only real sends bump `sent`.)
        zoho = get_batch_status("zoho")
        assert zoho["mailbox"] == "zoho"
        assert zoho["total"] == 1
        assert zoho["running"] is False

        # Aggregate exposes both channels; finished runs don't show as running.
        agg = get_batch_status()
        assert set(agg["profiles"].keys()) == {"default", "zoho"}
        assert agg["running"] is False

    def test_run_zoho_daily_batch_drains_both_pools(self, batch_db, monkeypatch):
        """run_zoho_daily_batch sends 孚盟数据 first, then 孚盟数据普通 via Zoho."""
        from emailer.batch import run_zoho_daily_batch

        monkeypatch.setattr("utils.email_verify.has_mx_record", lambda domain: True)
        conn = sqlite3.connect(str(batch_db))
        for name, src, email in [
            ("Fumu Hi", "孚盟数据", "a@fumu.com"),
            ("Fumu Norm", "孚盟数据普通", "b@fumu.com"),
        ]:
            conn.execute(
                "INSERT INTO leads (company_name, industry, country, email, status, source) "
                "VALUES (?, 'Automotive & Vehicles', 'Germany', ?, 'new', ?)",
                (name, email, src),
            )
        conn.commit()
        conn.close()

        res = run_zoho_daily_batch(dry_run=True)
        assert [p["source"] for p in res["pools"]] == ["孚盟数据", "孚盟数据普通"]
        assert res["sent"] == 2
        assert res["errors"] == 0

    def test_send_batch_mx_prefilter_fills_from_valid_pool(self, batch_db, monkeypatch):
        """MX pre-filter lands a limit=4 batch on 4 real (MX-valid) leads even
        when most of the pool is on no-MX domains — instead of selecting 4
        leads and skipping them all at send time."""
        from emailer.batch import send_batch, get_batch_status

        def mock_mx(domain):
            return domain != "no-mx.com"

        monkeypatch.setattr("utils.email_verify.has_mx_record", mock_mx)

        conn = sqlite3.connect(str(batch_db))
        for i in range(6):
            _insert_lead(conn, f"Good {i}", "Automotive & Vehicles", "Germany",
                         f"g{i}@good.com")
        for i in range(4):
            _insert_lead(conn, f"Bad {i}", "Automotive & Vehicles", "Germany",
                         f"b{i}@no-mx.com")
        conn.commit()
        conn.close()

        result = send_batch(limit=4, dry_run=True)
        companies = {d["company"] for d in result["details"]}
        assert len(companies) == 4
        assert all(c.startswith("Good") for c in companies)
        # No no-MX lead survives into the send list
        assert all(d.get("email", "").endswith("@good.com") for d in result["details"])
        # Progress reflects the filtered list, not the 10-lead candidate pool
        # (aggregate get_batch_status() sums ALL profiles — read "default" only)
        assert get_batch_status("default")["total"] == 4

    def test_send_batch_mx_prefilter_short_pool_sends_fewer(self, batch_db, monkeypatch):
        """When only 2 of many candidates pass MX, a limit=10 batch sends 2."""
        from emailer.batch import send_batch, get_batch_status

        def mock_mx(domain):
            return domain == "good.com"

        monkeypatch.setattr("utils.email_verify.has_mx_record", mock_mx)

        conn = sqlite3.connect(str(batch_db))
        for i in range(2):
            _insert_lead(conn, f"Good {i}", "Automotive & Vehicles", "Germany",
                         f"g{i}@good.com")
        for i in range(8):
            _insert_lead(conn, f"Bad {i}", "Automotive & Vehicles", "Germany",
                         f"b{i}@no-mx.com")
        conn.commit()
        conn.close()

        result = send_batch(limit=10, dry_run=True)
        assert len(result["details"]) == 2
        assert get_batch_status("default")["total"] == 2


# ---------------------------------------------------------------------------
# DB sync
# ---------------------------------------------------------------------------


class TestSyncTemplates:
    def test_sync_inserts_and_is_idempotent(self, batch_db):
        first = sync_templates_to_db(batch_db)
        assert first["total"] == 25
        assert first["inserted"] == 25

        conn = sqlite3.connect(str(batch_db))
        rows = conn.execute(
            "SELECT COUNT(*) FROM email_templates WHERE template_key IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        assert rows == 25

        second = sync_templates_to_db(batch_db)
        assert second["inserted"] == 0

    def test_sync_deactivates_legacy_seeds(self, batch_db):
        sync_templates_to_db(batch_db)
        conn = sqlite3.connect(str(batch_db))
        legacy_active = conn.execute(
            "SELECT COUNT(*) FROM email_templates "
            "WHERE is_active = 1 AND template_key IS NULL"
        ).fetchone()[0]
        conn.close()
        assert legacy_active == 0
