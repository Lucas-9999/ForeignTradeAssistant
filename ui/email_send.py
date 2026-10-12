"""
Email Send page — single send, batch send with progress, and campaign launch.
"""

import asyncio
import time
import streamlit as st

from crm.manager import LeadManager, TemplateManager, CommunicationManager
from crm.models import Communication
from emailer.templates import EmailTemplateRenderer
from emailer.rotation import AntiSpamGuard
from services.email_reputation.diagnostic_safety import sanitize_diagnostic
from services.email_reputation.sending_guard import build_sending_guard
from ui.components import get_conn, success_toast, error_toast, empty_state


def render_email_send():
    """Render the Email Send page with single + batch modes."""
    st.title("📧 Send Email")

    conn = get_conn()
    try:
        tab1, tab2, tab3 = st.tabs(["✉️ Single Send", "📬 Batch Send", "📊 Send Status"])

        with tab1:
            _render_single_send(conn)
        with tab2:
            _render_batch_send(conn)
        with tab3:
            _render_send_status()

    finally:
        conn.close()


# ── Single Send ────────────────────────────────────────────────────────


def _render_single_send(conn):
    """Single email send with template preview."""
    lead_mgr = LeadManager(conn)
    tmpl_mgr = TemplateManager(conn)
    guard = AntiSpamGuard(conn)
    sending_guard = build_sending_guard(
        conn, mailbox="default", anti_spam_guard=guard
    )
    renderer = EmailTemplateRenderer()

    quota = guard.get_status()
    st.metric("Remaining Today", quota["remaining"])

    st.divider()

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("1. Select Recipient")
        leads = lead_mgr.list_all(status="new", limit=200)
        if not leads:
            empty_state("No new leads available for sending. Run collection first.")
            return

        # Check for pre-selected batch IDs from leads page
        pre_selected = st.session_state.pop("email_batch_ids", None)
        if pre_selected:
            # Filter leads to only pre-selected ones
            pre_leads = [l for l in leads if l.id in pre_selected]
            if pre_leads:
                leads = pre_leads

        lead_options = {
            f"#{l.id} {l.company_name or '?'} ({l.email or 'no email'})": l.id
            for l in leads if l.email
        }

        if not lead_options:
            empty_state("No leads with email addresses found")
            return

        selected_label = st.selectbox("Lead", list(lead_options.keys()), key="single_lead")
        selected_lead_id = lead_options[selected_label]
        lead = lead_mgr.get_by_id(selected_lead_id)

    with col2:
        st.subheader("2. Select Template")
        templates = tmpl_mgr.list_active()
        if not templates:
            empty_state("No active templates")
            return
        tmpl_options = {f"{t.name} ({t.category})": t.id for t in templates}
        selected_tmpl_label = st.selectbox("Template", list(tmpl_options.keys()), key="single_tmpl")
        selected_tmpl_id = tmpl_options[selected_tmpl_label]

    st.divider()
    st.subheader("3. Preview & Send")

    if lead and selected_tmpl_id:
        tmpl = tmpl_mgr.get_by_id(selected_tmpl_id)
        preview = renderer.preview(tmpl.subject_template, tmpl.body_template)

        st.markdown(f"**To:** {lead.email}{' (' + lead.contact_name + ')' if lead.contact_name else ''}")
        st.markdown(f"**From template:** {tmpl.name}")
        subject_text = st.text_input("Subject", value=preview["subject"], key="single_subject")

        with st.expander("📄 Email Preview", expanded=True):
            st.html(preview["body"])

        # Custom body editing
        custom_body = st.text_area(
            "Edit HTML body (optional)",
            value=preview["body"],
            height=150,
            key="single_body_edit",
        )
        use_custom = st.checkbox("Use edited body", key="use_custom_body")

        if st.button("🚀 Send Email", type="primary", key="single_send_btn", use_container_width=True):
            comm_mgr = CommunicationManager(conn)
            if comm_mgr.has_outbound_to(selected_lead_id):
                error_toast(f"⚠️ Lead #{selected_lead_id} has already been contacted")
            else:
                body_to_send = custom_body if use_custom else preview["body"]
                with st.spinner("Sending..."):
                    async def do_send():
                        from emailer.send_core import SendIntent, send_one_async

                        return await send_one_async(
                            SendIntent(
                                email=lead.email,
                                subject=subject_text,
                                body_html=body_to_send,
                                to_name=lead.contact_name,
                                lead_id=lead.id,
                                source="ui:email_send",
                            ),
                            sending_guard=sending_guard,
                        )

                    outcome = asyncio.run(do_send())
                    success = outcome.success
                    message = outcome.message
                    msg_id = outcome.message_id

                    if outcome.blocked:
                        error_toast(f"⛔ Blocked: {outcome.message}")
                        return
                    if outcome.dry_run:
                        error_toast("🛡️ Global dry-run is active; email was not sent")
                        return

                    if success:
                        guard.record_send(lead.email)
                        tmpl_mgr.record_usage(selected_tmpl_id)
                        lead_mgr.mark_contacted(selected_lead_id)

                        comm_mgr2 = CommunicationManager(conn)
                        comm = Communication(
                            lead_id=lead.id,
                            template_id=selected_tmpl_id,
                            channel="email",
                            direction="outbound",
                            subject=subject_text,
                            body=body_to_send,
                            status="sent",
                            message_id=msg_id,
                        )
                        comm_mgr2.log(comm)
                        success_toast(f"✅ Sent to {lead.email}!")
                        st.balloons()
                    else:
                        guard.record_smtp_failure(
                            lead.email,
                            message,
                            provider="default",
                        )
                        message = sanitize_diagnostic(message)
                        error_toast(f"❌ Failed: {message}")


# ── Batch Send ─────────────────────────────────────────────────────────


def _render_batch_send(conn):
    """Batch email sending by country/industry segment (unified core)."""
    from emailer.batch import send_batch, segment_counts
    from emailer.segmentation import SegmentMatcher

    st.subheader("📬 Batch Email Sending")
    st.caption("Choose a segment → preview → send with per-lead template matching and anti-spam delays")

    matcher = SegmentMatcher()
    guard = AntiSpamGuard(conn)

    quota = guard.get_status()
    st.metric("Remaining Today", quota["remaining"], help="Daily sending limit")

    st.divider()
    st.markdown("#### Step 1: Segment Overview (new leads)")
    counts = segment_counts(status="new")
    ind_cols = st.columns(4)
    for idx, (group, n) in enumerate(
        sorted(counts["industry_groups"].items(), key=lambda kv: -kv[1])
    ):
        with ind_cols[idx % 4]:
            st.metric(matcher.industry_group_label(group), n)
    st.caption(
        f"Low-relevance industries (excluded unless enabled below): "
        f"{counts['low_relevance']}"
    )

    st.divider()
    st.markdown("#### Step 2: Choose Segment & Filters")

    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        status_filter = st.selectbox(
            "Status",
            ["new", "contacted", "replied"],
            key="batch_status",
            help="'new' recommended for first-contact sends.",
        )
    with col_f2:
        country_group = st.selectbox(
            "Country Group",
            ["All"] + matcher.country_group_names(),
            key="batch_country_group",
            help="Auto-matches the right letter style per market.",
        )
    with col_f3:
        industry_group = st.selectbox(
            "Industry Group",
            ["All"] + matcher.industry_group_names(),
            key="batch_industry_group",
            help="Auto-matches the industry-specific letter.",
        )

    col_g1, col_g2 = st.columns(2)
    with col_g1:
        include_low = st.checkbox(
            "Include low-relevance industries",
            key="batch_include_low",
            help="Food, Footwear, Toys, etc. - sent after core industries or when enabled.",
        )
    with col_g2:
        country_filter = st.text_input(
            "Country keyword (optional)",
            placeholder="e.g. Germany",
            key="batch_country_kw",
        )

    st.divider()
    st.markdown("#### Step 3: Preview")

    dry_run = st.checkbox(
        "Dry run (preview only, no actual send)",
        key="batch_dry_run",
        help="Check this to preview what would be sent.",
    )
    if st.button("🔍 Preview Matched Emails", key="batch_preview_btn"):
        preview = send_batch(
            limit=3,
            dry_run=True,
            status=status_filter,
            country_group=country_group if country_group != "All" else None,
            industry_group=industry_group if industry_group != "All" else None,
            include_low_relevance=include_low,
            country_filter=country_filter or None,
        )
        if preview.get("fatal"):
            error_toast(preview["fatal"])
        elif not preview.get("details"):
            st.info("No eligible leads match this segment.")
        else:
            for d in preview["details"]:
                st.markdown(f"**{d.get('company', '?')}** ({d.get('email', '')})")
                st.caption(
                    f"Template: {d.get('template_name', '')} | "
                    f"Subject: {d.get('subject', '')[:80]}"
                )

    st.divider()
    st.markdown("#### Step 4: Execute")

    limit = st.slider(
        "Max to send",
        1,
        200,
        min(10, max(quota["remaining"], 1)),
        key="batch_limit",
    )
    if st.button(
        "🚀 Start Batch Send",
        type="primary",
        key="batch_start_btn",
        use_container_width=True,
    ):
        with st.spinner("Sending..."):
            results = send_batch(
                limit=limit,
                dry_run=dry_run,
                status=status_filter,
                country_group=country_group if country_group != "All" else None,
                industry_group=industry_group if industry_group != "All" else None,
                include_low_relevance=include_low,
                country_filter=country_filter or None,
            )
        col1, col2, col3 = st.columns(3)
        col1.metric("✅ Sent", results.get("sent", 0))
        col2.metric("⏭️ Skipped", results.get("skipped", 0))
        col3.metric("❌ Errors", results.get("errors", 0))
        if results.get("fatal"):
            error_toast(results["fatal"])
        if results.get("details"):
            st.subheader("Detail Log")
            items = []
            for d in results["details"]:
                icon = {
                    "sent": "✅",
                    "dry_run": "📋",
                    "skipped": "⏭️",
                    "error": "❌",
                }.get(d["status"], "❓")
                note = (
                    d.get("reason")
                    or d.get("template_name")
                    or d.get("subject", "")
                )
                items.append(
                    f"{icon} **{d.get('company', '?')}** "
                    f"({d.get('email', '')}) — {note}"
                )
            st.markdown("\n\n".join(items[:60]))


# ── Send Status ────────────────────────────────────────────────────────


def _render_send_status():
    """Show current batch send status from background jobs."""
    st.subheader("📊 Send Status")
    try:
        from emailer.batch import get_batch_status
        status = get_batch_status()
    except ImportError:
        status = {}

    running_profiles = {
        k: v for k, v in (status.get("profiles") or {}).items() if v.get("running")
    }

    if status.get("running"):
        if len(running_profiles) > 1:
            st.info(f"🔄 {len(running_profiles)} batch sends running concurrently")
        else:
            st.info("🔄 A batch send is currently running")
        st.metric("Progress", f"{status.get('sent', 0)} / {status.get('total', 0)}")
        if status.get("current_company"):
            st.caption(f"Sending to: **{status['current_company']}** ({status.get('current_email', '')})")
        st.progress(status.get("sent", 0) / max(status.get("total", 1), 1))
        # Per-mailbox breakdown (NetEase 200/day + Zoho 20/day run in parallel).
        if len(running_profiles) > 1:
            st.markdown("**Per-mailbox:**")
            for key, p in running_profiles.items():
                st.caption(
                    f"`{key}` — {p.get('sent', 0)}/{p.get('total', 0)} "
                    f"→ {p.get('current_company', '') or '…'}"
                )
    else:
        st.success("No batch send currently running")
        if status.get("sent"):
            st.metric("Last Batch", f"{status.get('sent', 0)} sent")
