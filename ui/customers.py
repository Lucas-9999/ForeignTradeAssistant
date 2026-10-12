"""
Customers page — view, create, and manage customer records.
"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta

from crm.manager import CustomerManager, LeadManager
from crm.models import Customer
from ui.components import (
    get_conn, status_badge, empty_state, success_toast, error_toast,
    confirm_action,
)


def render_customers():
    """Render the Customers management page with create/edit/delete."""
    st.title("🏢 Customer Management")

    conn = get_conn()
    try:
        cust_mgr = CustomerManager(conn)

        tab1, tab2, tab3 = st.tabs(["📋 Active Customers", "➕ Add Customer", "⚠️ Overdue"])

        with tab1:
            _render_customer_list(cust_mgr)
        with tab2:
            _render_add_customer_form(cust_mgr)
        with tab3:
            _render_overdue_followups(cust_mgr)

    finally:
        conn.close()


def _render_customer_list(cust_mgr: CustomerManager):
    """List active customers with search and quick actions."""
    st.subheader("All Active Customers")

    search = st.text_input("🔍 Search", placeholder="Company, contact, or email...",
                           key="cust_search")

    customers = cust_mgr.list_all(search=search or None, is_active=1, limit=200)

    if not customers:
        empty_state("No active customers" if not search else "No customers match your search")
        return

    st.caption(f"{len(customers)} customers")

    # Build table
    df_rows = []
    for c in customers:
        df_rows.append({
            "ID": c.id,
            "Company": c.company_name,
            "Contact": c.contact_name or "—",
            "Email": c.email or "—",
            "Country": c.country or "—",
            "Channel": c.preferred_channel,
            "Follow-up": c.next_followup_at or "—",
            "Orders": c.total_orders,
        })

    df = pd.DataFrame(df_rows)
    st.dataframe(df, use_container_width=True, height=400)

    # Quick actions
    st.divider()
    st.subheader("Quick Actions")

    cust_options = {f"#{c.id} — {c.company_name}": c.id for c in customers}
    selected_label = st.selectbox("Select Customer", list(cust_options.keys()), key="cust_select")
    cust_id = cust_options[selected_label]

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("📜 Communication History", key="cust_history", use_container_width=True):
            history = cust_mgr.get_communication_history(cust_id)
            if history:
                st.dataframe(pd.DataFrame(history), use_container_width=True)
            else:
                st.info("No communication history")

    with col2:
        if st.button("✅ Record Contact", key="cust_contact", use_container_width=True):
            cust_mgr.record_contact(cust_id)
            success_toast("Follow-up date updated")
            st.rerun()

    with col3:
        if st.button("🗑️ Deactivate", key="cust_deactivate", use_container_width=True):
            if confirm_action("Deactivate Customer", "They will become inactive", f"deact_{cust_id}"):
                cust_mgr.delete(cust_id)
                success_toast(f"Customer #{cust_id} deactivated")
                st.rerun()

    # Edit form for selected customer
    st.divider()
    st.subheader("✏️ Edit Customer")
    customer = cust_mgr.get_by_id(cust_id)
    if customer:
        with st.form(f"edit_cust_form_{cust_id}"):
            col1, col2 = st.columns(2)
            with col1:
                company = st.text_input("Company Name", value=customer.company_name or "")
                contact = st.text_input("Contact Name", value=customer.contact_name or "")
                email = st.text_input("Email", value=customer.email or "")
                phone = st.text_input("Phone", value=customer.phone or "")
            with col2:
                country = st.text_input("Country", value=customer.country or "")
                industry = st.text_input("Industry", value=customer.industry or "")
                channel = st.selectbox(
                    "Preferred Channel",
                    ["email", "whatsapp", "both"],
                    index=["email", "whatsapp", "both"].index(customer.preferred_channel)
                    if customer.preferred_channel in ["email", "whatsapp", "both"] else 0,
                )
                interval = st.number_input("Follow-up Interval (days)", value=customer.followup_interval_days,
                                           min_value=1, max_value=365)

            notes = st.text_area("Notes", value=customer.notes or "", height=80)

            if st.form_submit_button("💾 Save Changes", type="primary"):
                customer.company_name = company
                customer.contact_name = contact or None
                customer.email = email or None
                customer.phone = phone or None
                customer.country = country or None
                customer.industry = industry or None
                customer.preferred_channel = channel
                customer.followup_interval_days = interval
                customer.notes = notes or None
                cust_mgr.update(customer)
                success_toast(f"Customer #{cust_id} updated!")
                st.rerun()


def _render_add_customer_form(cust_mgr: CustomerManager):
    """Add a new customer (manual creation or promoted from lead)."""
    st.subheader("➕ Add New Customer")

    # Option to promote from existing lead
    conn = cust_mgr.conn
    lead_mgr = LeadManager(conn)

    promote_source = st.radio(
        "Creation method",
        ["Create manually", "Promote from existing lead"],
        key="cust_create_method",
        horizontal=True,
    )

    if promote_source == "Promote from existing lead":
        leads = lead_mgr.list_all(limit=200)
        lead_options = {
            f"#{l.id} — {l.company_name or '?'} ({l.email or 'no email'}) — {l.status}": l.id
            for l in leads
        }
        if not lead_options:
            st.info("No leads available to promote")
            return
        selected_label = st.selectbox("Select Lead to Promote", list(lead_options.keys()))
        selected_lead_id = lead_options[selected_label]

        if st.button("⬆️ Promote to Customer", type="primary"):
            cust_id = lead_mgr.promote_to_customer(selected_lead_id)
            if cust_id:
                success_toast(f"Lead #{selected_lead_id} promoted → Customer #{cust_id}")
                st.rerun()
            else:
                error_toast("Promotion failed")
        return

    # Manual creation
    with st.form("add_customer_form"):
        col1, col2 = st.columns(2)
        with col1:
            company = st.text_input("Company Name *")
            contact = st.text_input("Contact Name")
            email = st.text_input("Email")
            phone = st.text_input("Phone")
            whatsapp = st.text_input("WhatsApp")
        with col2:
            country = st.text_input("Country")
            industry = st.text_input("Industry")
            channel = st.selectbox("Preferred Channel", ["email", "whatsapp", "both"])
            interval = st.number_input("Follow-up Interval (days)", value=30, min_value=1, max_value=365)

        notes = st.text_area("Notes", height=80)
        tags_input = st.text_input("Tags (comma-separated)", placeholder="e.g. vip, urgent, large-order")

        if st.form_submit_button("➕ Create Customer", type="primary"):
            if not company:
                error_toast("Company name is required")
            else:
                customer = Customer(
                    company_name=company,
                    contact_name=contact or None,
                    email=email or None,
                    phone=phone or None,
                    whatsapp=whatsapp or None,
                    country=country or None,
                    industry=industry or None,
                    preferred_channel=channel,
                    followup_interval_days=interval,
                    notes=notes or None,
                    tags=tags_input or None,
                )
                customer = cust_mgr.create(customer)
                success_toast(f"Customer #{customer.id} created: {company}")
                st.rerun()


def _render_overdue_followups(cust_mgr: CustomerManager):
    """Display customers overdue for follow-up."""
    st.subheader("⚠️ Overdue Follow-ups")

    overdue = cust_mgr.get_overdue_followups()
    if overdue:
        st.warning(f"{len(overdue)} customers overdue for follow-up!")
        df_rows = []
        for c in overdue:
            df_rows.append({
                "ID": c.id,
                "Company": c.company_name,
                "Contact": c.contact_name or "—",
                "Email": c.email or "—",
                "Next Follow-up": c.next_followup_at or "—",
                "Days Overdue": _days_overdue(c.next_followup_at),
            })
        st.dataframe(pd.DataFrame(df_rows), use_container_width=True)

        if st.button("📧 Mark All as Contacted Today", type="primary", key="mark_all_contacted"):
            for c in overdue:
                cust_mgr.record_contact(c.id)
            success_toast(f"Updated {len(overdue)} customers")
            st.rerun()
    else:
        st.success("All follow-ups up to date! 🎉")


def _days_overdue(followup_date_str: str) -> int:
    """Calculate days between today and a followup date string."""
    if not followup_date_str:
        return 0
    try:
        from datetime import datetime
        followup = datetime.fromisoformat(followup_date_str).date()
        return (date.today() - followup).days
    except Exception:
        return 0
