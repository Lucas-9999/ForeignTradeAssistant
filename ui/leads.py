"""
Leads page — CRUD, batch operations, edit form, search, and filtering.
"""

import streamlit as st
import pandas as pd

from crm.manager import LeadManager
from crm.models import Lead
from ui.components import (
    get_conn, status_badge, build_lead_dataframe,
    confirm_action, empty_state, success_toast, error_toast,
)


def render_leads():
    """Render the Leads management page with batch ops and editing."""
    st.title("👤 Lead Management")

    conn = get_conn()
    try:
        lead_mgr = LeadManager(conn)

        tab1, tab2, tab3 = st.tabs(["📋 Browse", "➕ Add Lead", "✏️ Edit / Dedup"])

        # ═══════════════════════════════════════════════════════════
        # TAB 1: Browse + Batch Operations
        # ═══════════════════════════════════════════════════════════
        with tab1:
            _render_browse_tab(lead_mgr)

        # ═══════════════════════════════════════════════════════════
        # TAB 2: Add Lead
        # ═══════════════════════════════════════════════════════════
        with tab2:
            _render_add_lead_form(lead_mgr)

        # ═══════════════════════════════════════════════════════════
        # TAB 3: Edit + Dedup
        # ═══════════════════════════════════════════════════════════
        with tab3:
            _render_edit_dedup_tab(lead_mgr)

    finally:
        conn.close()


# ── Tab 1: Browse ──────────────────────────────────────────────────────


def _render_browse_tab(lead_mgr: LeadManager):
    """Browse leads with filters, batch selection, and quick actions."""
    from emailer.batch import segment_counts
    from emailer.segmentation import SegmentMatcher

    matcher = SegmentMatcher()

    # Filters
    col1, col2, col3 = st.columns(3)
    with col1:
        status_filter = st.selectbox(
            "Status Filter",
            ["All", "new", "contacted", "replied", "customer", "dead"],
            key="lead_status_filter",
        )
    with col2:
        source_filter = st.selectbox(
            "Source Filter",
            ["All", "manual", "facebook", "industry_site", "xunkexing", "孚盟数据"],
            key="lead_source_filter",
        )
    with col3:
        search = st.text_input("🔍 Search", placeholder="Company, email, contact...",
                               key="lead_search")

    col4, col5 = st.columns(2)
    with col4:
        country_group_filter = st.selectbox(
            "Country Group",
            ["All"] + matcher.country_group_names(),
            key="lead_country_group",
        )
    with col5:
        industry_group_filter = st.selectbox(
            "Industry Group",
            ["All"] + matcher.industry_group_names(),
            key="lead_industry_group",
        )

    # Segment counts (new leads) - quick overview
    counts = segment_counts(status="new")
    with st.expander("📊 Segment Overview (new leads)"):
        ind_cols = st.columns(4)
        for idx, (group, n) in enumerate(
            sorted(counts["industry_groups"].items(), key=lambda kv: -kv[1])
        ):
            with ind_cols[idx % 4]:
                st.metric(matcher.industry_group_label(group), n)
        st.caption(f"Low-relevance industries: {counts['low_relevance']}")
        ctry_cols = st.columns(4)
        for idx, (group, n) in enumerate(
            sorted(counts["country_groups"].items(), key=lambda kv: -kv[1])
        ):
            with ctry_cols[idx % 4]:
                st.metric(matcher.country_group_label(group), n)

    status = None if status_filter == "All" else status_filter
    source = None if source_filter == "All" else source_filter

    leads = lead_mgr.list_all(
        status=status,
        source=source,
        search=search or None,
        limit=300,
    )

    # Client-side segment filters (fetched window only)
    if country_group_filter != "All" and leads:
        leads = [
            l for l in leads
            if matcher.country_group(l.country) == country_group_filter
        ]
    if industry_group_filter != "All" and leads:
        leads = [
            l for l in leads
            if matcher.industry_group(l.industry)[0] == industry_group_filter
        ]

    if not leads:
        empty_state("No leads match your filters. Try adjusting the criteria.")
        return

    st.caption(f"{len(leads)} leads found")

    # ── Batch selection ─────────────────────────────────────────────
    lead_map = {f"#{l.id} — {l.company_name or 'Unknown'} ({l.email or 'no email'})": l.id
                for l in leads}

    selected_labels = st.multiselect(
        "Select leads for batch actions",
        list(lead_map.keys()),
        key="batch_select",
        help="Select one or more leads to perform batch operations",
    )
    selected_ids = [lead_map[label] for label in selected_labels]

    if selected_ids:
        _render_batch_actions(lead_mgr, selected_ids, selected_labels)

    # ── Lead table ──────────────────────────────────────────────────
    st.divider()
    st.subheader("Lead List")

    df = build_lead_dataframe(leads)
    if not df.empty:
        # Colorize status column if present
        st.dataframe(df, use_container_width=True, height=400,
                     column_config={
                         "Status": st.column_config.TextColumn(width="small"),
                         "ID": st.column_config.NumberColumn(width="small"),
                     })

    # ── Quick actions on single lead ────────────────────────────────
    st.divider()
    st.subheader("⚡ Quick Actions")

    col_sel, col_act = st.columns(2)
    with col_sel:
        lead_options = list(lead_map.keys())
        selected_label = st.selectbox("Select a lead", lead_options, key="quick_action_select")
        lead_id = lead_map[selected_label]

    with col_act:
        action = st.selectbox("Action", [
            "View Details",
            "Mark Contacted",
            "Mark Replied",
            "Mark Dead",
            "Promote to Customer",
            "Delete",
        ], key="quick_action_type")

    if st.button("Execute Action", type="primary"):
        with st.spinner():
            if action == "View Details":
                lead = lead_mgr.get_by_id(lead_id)
                if lead:
                    st.json(lead.to_dict())

            elif action == "Mark Contacted":
                lead_mgr.mark_contacted(lead_id)
                success_toast(f"Lead #{lead_id} marked as contacted")
                st.rerun()

            elif action == "Mark Replied":
                lead_mgr.mark_replied(lead_id)
                success_toast(f"Lead #{lead_id} marked as replied")
                st.rerun()

            elif action == "Mark Dead":
                lead_mgr.mark_dead(lead_id)
                success_toast(f"Lead #{lead_id} marked as dead")
                st.rerun()

            elif action == "Promote to Customer":
                cust_id = lead_mgr.promote_to_customer(lead_id)
                if cust_id:
                    success_toast(f"Lead #{lead_id} promoted → Customer #{cust_id}")
                    st.rerun()
                else:
                    error_toast("Promotion failed — lead not found")

            elif action == "Delete":
                lead = lead_mgr.get_by_id(lead_id)
                company = lead.company_name if lead else f"#{lead_id}"
                if confirm_action(f"Delete {company}", "This is permanent!", f"del_{lead_id}"):
                    lead_mgr.delete(lead_id)
                    success_toast(f"Lead #{lead_id} deleted")
                    st.rerun()


def _render_batch_actions(lead_mgr: LeadManager, selected_ids: list[int], selected_labels: list[str]):
    """Render batch action buttons for selected leads."""
    st.info(f"**{len(selected_ids)} leads selected**")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        if st.button("📨 Mark All Contacted", key="batch_contacted", use_container_width=True):
            with st.spinner():
                for lid in selected_ids:
                    lead_mgr.mark_contacted(lid)
                success_toast(f"{len(selected_ids)} leads marked as contacted")
                st.rerun()

    with col2:
        if st.button("🗑️ Mark All Dead", key="batch_dead", use_container_width=True):
            if confirm_action(f"Mark {len(selected_ids)} as Dead",
                              "This removes them from active pipeline",
                              "batch_dead_confirm"):
                with st.spinner():
                    for lid in selected_ids:
                        lead_mgr.mark_dead(lid)
                    success_toast(f"{len(selected_ids)} leads marked as dead")
                    st.rerun()

    with col3:
        if st.button("🚀 Send Email to All", key="batch_send", use_container_width=True):
            # Store selected IDs in session state and navigate to email tab
            st.session_state.email_batch_ids = selected_ids
            st.info("→ Switch to the **📧 Send Email** tab to configure and send")
            # Display the selected leads
            for label in selected_labels:
                st.caption(f"  {label}")

    with col4:
        if st.button("❌ Delete All", key="batch_delete", use_container_width=True):
            if confirm_action(f"PERMANENTLY DELETE {len(selected_ids)} leads",
                              "This CANNOT be undone!", "batch_del_confirm"):
                with st.spinner():
                    for lid in selected_ids:
                        lead_mgr.delete(lid)
                    success_toast(f"{len(selected_ids)} leads deleted")
                    st.rerun()


# ── Tab 2: Add Lead ────────────────────────────────────────────────────


def _render_add_lead_form(lead_mgr: LeadManager):
    """Add new lead form."""
    st.subheader("Add New Lead")
    with st.form("add_lead_form"):
        col1, col2 = st.columns(2)
        with col1:
            company = st.text_input("Company Name *")
            contact = st.text_input("Contact Name")
            email = st.text_input("Email")
            phone = st.text_input("Phone")
            whatsapp = st.text_input("WhatsApp")
        with col2:
            website = st.text_input("Website")
            country = st.text_input("Country")
            industry = st.text_input("Industry")
            source = st.selectbox("Source", ["manual", "facebook", "industry_site", "xunkexing", "孚盟数据"])
            source_url = st.text_input("Source URL")

        notes = st.text_area("Notes", height=80)

        col_submit, _ = st.columns([1, 3])
        with col_submit:
            submitted = st.form_submit_button("➕ Add Lead", type="primary", use_container_width=True)

        if submitted:
            if not company:
                error_toast("Company name is required")
            else:
                # Basic email validation
                if email and "@" not in email:
                    error_toast("Invalid email format")
                else:
                    lead = Lead(
                        company_name=company,
                        contact_name=contact or None,
                        email=email or None,
                        phone=phone or None,
                        whatsapp=whatsapp or None,
                        website=website or None,
                        country=country or None,
                        industry=industry or None,
                        source=source,
                        source_url=source_url or None,
                        notes=notes or None,
                    )
                    lead = lead_mgr.create(lead)
                    success_toast(f"Lead #{lead.id} created: {company}")
                    st.rerun()


# ── Tab 3: Edit + Dedup ────────────────────────────────────────────────


def _render_edit_dedup_tab(lead_mgr: LeadManager):
    """Edit existing leads and manage duplicates."""
    st.subheader("✏️ Edit Existing Lead")

    # Select lead to edit
    leads = lead_mgr.list_all(limit=300)
    if not leads:
        empty_state("No leads to edit")
        return

    lead_map = {f"#{l.id} — {l.company_name or 'Unknown'}": l for l in leads}
    selected_label = st.selectbox(
        "Choose lead to edit",
        list(lead_map.keys()),
        key="edit_lead_select",
    )
    lead = lead_map[selected_label]

    if not lead or not lead.id:
        return

    with st.form("edit_lead_form"):
        st.caption(f"Editing Lead #{lead.id} — created {lead.created_at}")

        col1, col2 = st.columns(2)
        with col1:
            company = st.text_input("Company Name", value=lead.company_name or "")
            contact = st.text_input("Contact Name", value=lead.contact_name or "")
            email = st.text_input("Email", value=lead.email or "")
            phone = st.text_input("Phone", value=lead.phone or "")
            whatsapp = st.text_input("WhatsApp", value=lead.whatsapp or "")
            source = st.selectbox(
                "Source",
                ["manual", "facebook", "industry_site", "xunkexing", "孚盟数据"],
                index=["manual", "facebook", "industry_site", "xunkexing", "孚盟数据"].index(lead.source)
                if lead.source in ["manual", "facebook", "industry_site", "xunkexing", "孚盟数据"] else 0,
            )
        with col2:
            website = st.text_input("Website", value=lead.website or "")
            country = st.text_input("Country", value=lead.country or "")
            industry = st.text_input("Industry", value=lead.industry or "")
            status = st.selectbox(
                "Status",
                ["new", "contacted", "replied", "customer", "dead"],
                index=["new", "contacted", "replied", "customer", "dead"].index(lead.status)
                if lead.status in ["new", "contacted", "replied", "customer", "dead"] else 0,
            )
            source_url = st.text_input("Source URL", value=lead.source_url or "")

        notes = st.text_area("Notes", value=lead.notes or "", height=80)

        col_save, col_cancel = st.columns(2)
        with col_save:
            submitted = st.form_submit_button("💾 Save Changes", type="primary", use_container_width=True)
        with col_cancel:
            cancelled = st.form_submit_button("↩ Reset", use_container_width=True)

        if submitted:
            if not company:
                error_toast("Company name is required")
            else:
                # Update fields on the lead object
                lead.company_name = company
                lead.contact_name = contact or None
                lead.email = email or None
                lead.phone = phone or None
                lead.whatsapp = whatsapp or None
                lead.website = website or None
                lead.country = country or None
                lead.industry = industry or None
                lead.source = source
                lead.source_url = source_url or None
                lead.status = status
                lead.notes = notes or None

                lead_mgr.update(lead)
                success_toast(f"Lead #{lead.id} updated!")
                st.rerun()

    # ── Duplicate detection ──────────────────────────────────────────
    st.divider()
    st.subheader("🔍 Duplicate Detection")

    dupes = lead_mgr.find_duplicates()
    if dupes:
        st.warning(f"Found {len(dupes)} email(s) with duplicate leads:")
        for d in dupes:
            with st.expander(f"{d['email']} — {d['count']} leads (IDs: {d['lead_ids']})"):
                # Show each duplicate
                for lid in d["lead_ids"]:
                    l = lead_mgr.get_by_id(lid)
                    if l:
                        st.text(f"  #{l.id}: {l.company_name} ({l.status})")

                # Merge UI
                if len(d["lead_ids"]) >= 2:
                    keep_id = st.selectbox(
                        f"Keep which lead?",
                        d["lead_ids"],
                        key=f"merge_keep_{d['email']}",
                    )
                    merge_options = [x for x in d["lead_ids"] if x != keep_id]
                    for merge_id in merge_options:
                        if st.button(f"Merge #{merge_id} → #{keep_id}", key=f"merge_{merge_id}"):
                            lead_mgr.merge_duplicates(keep_id, merge_id)
                            success_toast(f"Merged #{merge_id} into #{keep_id}")
                            st.rerun()
    else:
        st.success("No duplicate emails detected.")
