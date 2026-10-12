"""
Tests for CRM module — LeadManager, CustomerManager, TemplateManager.
"""

import pytest

from crm.manager import (
    LeadManager,
    CustomerManager,
    TemplateManager,
    CommunicationManager,
    BlacklistManager,
)
from crm.models import Lead, Customer, EmailTemplate, Communication


class TestLeadManager:
    """Tests for Lead CRUD operations."""

    def test_create_lead(self, db_conn, sample_lead_data):
        mgr = LeadManager(db_conn)
        lead = Lead(**sample_lead_data)
        lead = mgr.create(lead)

        assert lead.id is not None
        assert lead.id > 0
        assert lead.company_name == "ACME Fasteners Inc."
        assert lead.status == "new"

    def test_get_by_id(self, db_conn, sample_lead_data):
        mgr = LeadManager(db_conn)
        lead = mgr.create(Lead(**sample_lead_data))

        fetched = mgr.get_by_id(lead.id)
        assert fetched is not None
        assert fetched.company_name == sample_lead_data["company_name"]
        assert fetched.email == sample_lead_data["email"]

    def test_get_nonexistent(self, db_conn):
        mgr = LeadManager(db_conn)
        assert mgr.get_by_id(9999) is None

    def test_list_all(self, db_conn, sample_lead_data):
        mgr = LeadManager(db_conn)
        mgr.create(Lead(**sample_lead_data))
        mgr.create(Lead(company_name="Beta Corp", email="beta@test.com", source="manual"))

        leads = mgr.list_all()
        assert len(leads) == 2

        # Filter by status
        new_leads = mgr.list_all(status="new")
        assert len(new_leads) == 2

        dead_leads = mgr.list_all(status="dead")
        assert len(dead_leads) == 0

    def test_list_with_search(self, db_conn):
        mgr = LeadManager(db_conn)
        mgr.create(Lead(company_name="ACME Corp", email="acme@test.com", source="manual"))
        mgr.create(Lead(company_name="Beta LLC", email="beta@test.com", source="manual"))

        results = mgr.list_all(search="ACME")
        assert len(results) == 1
        assert results[0].company_name == "ACME Corp"

    def test_duplicate_detection(self, db_conn, sample_lead_data):
        mgr = LeadManager(db_conn)
        lead1 = mgr.create(Lead(**sample_lead_data))
        lead2 = mgr.create(Lead(**sample_lead_data))  # same email

        assert lead2.status == "dead"
        assert lead2.duplicate_of == lead1.id

    def test_mark_contacted(self, db_conn, sample_lead_data):
        mgr = LeadManager(db_conn)
        lead = mgr.create(Lead(**sample_lead_data))

        mgr.mark_contacted(lead.id)
        updated = mgr.get_by_id(lead.id)
        assert updated.status == "contacted"
        assert updated.first_contacted_at is not None
        assert updated.last_contacted_at is not None

    def test_mark_replied(self, db_conn, sample_lead_data):
        mgr = LeadManager(db_conn)
        lead = mgr.create(Lead(**sample_lead_data))

        mgr.mark_replied(lead.id)
        updated = mgr.get_by_id(lead.id)
        assert updated.status == "replied"

    def test_mark_dead(self, db_conn, sample_lead_data):
        mgr = LeadManager(db_conn)
        lead = mgr.create(Lead(**sample_lead_data))

        mgr.mark_dead(lead.id)
        updated = mgr.get_by_id(lead.id)
        assert updated.status == "dead"

    def test_delete_lead(self, db_conn, sample_lead_data):
        mgr = LeadManager(db_conn)
        lead = mgr.create(Lead(**sample_lead_data))

        mgr.delete(lead.id)
        assert mgr.get_by_id(lead.id) is None

    def test_count_by_status(self, db_conn, sample_lead_data):
        mgr = LeadManager(db_conn)
        mgr.create(Lead(**sample_lead_data))
        mgr.create(Lead(company_name="B", email="b@test.com", source="manual"))

        counts = mgr.count_by_status()
        assert counts.get("new", 0) == 2

    def test_create_batch(self, db_conn):
        mgr = LeadManager(db_conn)
        leads = [
            Lead(company_name="A Corp", email="a@test.com", source="manual"),
            Lead(company_name="B Corp", email="b@test.com", source="manual"),
            Lead(company_name="A Corp", email="a@test.com", source="manual"),  # duplicate
        ]
        created, skipped = mgr.create_batch(leads)
        assert created == 2
        assert skipped == 1


class TestCustomerManager:
    """Tests for Customer CRUD operations."""

    def test_create_customer(self, db_conn):
        mgr = CustomerManager(db_conn)
        cust = Customer(company_name="Test Customer", email="test@test.com")
        cust = mgr.create(cust)

        assert cust.id is not None
        assert cust.company_name == "Test Customer"
        assert cust.next_followup_at is not None  # calculated on create

    def test_get_due_followups(self, db_conn):
        mgr = CustomerManager(db_conn)
        cust = mgr.create(Customer(
            company_name="Due Soon",
            email="due@test.com",
            followup_interval_days=0,  # due immediately
        ))

        due = mgr.get_due_followups(days_ahead=0)
        assert len(due) >= 1
        assert any(c.company_name == "Due Soon" for c in due)

    def test_get_overdue(self, db_conn):
        mgr = CustomerManager(db_conn)
        cust = mgr.create(Customer(
            company_name="Overdue",
            email="overdue@test.com",
            followup_interval_days=-1,  # overdue yesterday
        ))

        overdue = mgr.get_overdue_followups()
        assert any(c.company_name == "Overdue" for c in overdue)

    def test_record_contact(self, db_conn):
        mgr = CustomerManager(db_conn)
        cust = mgr.create(Customer(company_name="Test", email="test@test.com"))

        mgr.record_contact(cust.id)
        updated = mgr.get_by_id(cust.id)
        assert updated.last_contacted_at is not None
        assert updated.next_followup_at is not None

    def test_promote_lead_to_customer(self, db_conn):
        lead_mgr = LeadManager(db_conn)
        lead = lead_mgr.create(Lead(
            company_name="Promote Me",
            email="promote@test.com",
            source="manual",
            country="Germany",
        ))

        customer_id = lead_mgr.promote_to_customer(lead.id)
        assert customer_id is not None

        lead = lead_mgr.get_by_id(lead.id)
        assert lead.status == "customer"

        cust_mgr = CustomerManager(db_conn)
        cust = cust_mgr.get_by_id(customer_id)
        assert cust.company_name == "Promote Me"
        assert cust.lead_id == lead.id


class TestTemplateManager:
    """Tests for email template CRUD and rotation."""

    def test_create_template(self, db_conn):
        mgr = TemplateManager(db_conn)
        tmpl = EmailTemplate(
            name="Test Template",
            subject_template="Hello {{ company_name }}",
            body_template="<p>Hi {{ contact_name }}</p>",
            category="icebreaker",
        )
        tmpl = mgr.create(tmpl)
        assert tmpl.id is not None

    def test_list_active(self, db_conn):
        mgr = TemplateManager(db_conn)
        tmpl = mgr.create(EmailTemplate(
            name="Active A",
            subject_template="S1",
            body_template="B1",
            category="icebreaker",
        ))
        mgr.create(EmailTemplate(
            name="Inactive",
            subject_template="S2",
            body_template="B2",
            category="followup",
            is_active=0,
        ))

        active = mgr.list_active()
        assert len(active) >= 1

    def test_rotation_picks_least_used(self, db_conn):
        mgr = TemplateManager(db_conn)
        t1 = mgr.create(EmailTemplate(
            name="Rotation A",
            subject_template="SA",
            body_template="BA",
            category="icebreaker",
        ))
        t2 = mgr.create(EmailTemplate(
            name="Rotation B",
            subject_template="SB",
            body_template="BB",
            category="icebreaker",
        ))

        # Use t1 once
        mgr.record_usage(t1.id)

        # Rotation should pick t2 (never used)
        picked = mgr.select_for_rotation("icebreaker")
        assert picked is not None
        assert picked.name == "Rotation B"

    def test_record_usage(self, db_conn):
        mgr = TemplateManager(db_conn)
        tmpl = mgr.create(EmailTemplate(
            name="Usage Test",
            subject_template="S",
            body_template="B",
            category="icebreaker",
        ))

        mgr.record_usage(tmpl.id)
        updated = mgr.get_by_id(tmpl.id)
        assert updated.usage_count == 1
        assert updated.last_used_at is not None


class TestBlacklistManager:
    """Tests for blacklist operations."""

    def test_add_and_check(self, db_conn):
        mgr = BlacklistManager(db_conn)
        mgr.add("spam@test.com", "bounced")

        assert mgr.is_blocked("spam@test.com")
        assert mgr.is_blocked("SPAM@TEST.COM")  # case insensitive
        assert not mgr.is_blocked("clean@test.com")

    def test_add_duplicate(self, db_conn):
        mgr = BlacklistManager(db_conn)
        mgr.add("dupe@test.com", "bounced")
        mgr.add("dupe@test.com", "complaint")  # should not raise

        entries = mgr.list_all()
        assert len(entries) == 1
        assert entries[0].email == "dupe@test.com"

    def test_remove(self, db_conn):
        mgr = BlacklistManager(db_conn)
        mgr.add("remove@test.com", "bounced")
        mgr.remove("remove@test.com")

        assert not mgr.is_blocked("remove@test.com")
