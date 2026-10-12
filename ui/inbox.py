"""
Inbox page — IMAP inbox monitoring, reply detection, and recent replies.
"""

import streamlit as st
import pandas as pd

from ui.components import get_conn, empty_state, status_badge, success_toast, error_toast


def render_inbox():
    """Render the IMAP Inbox page with manual check trigger and recent replies."""
    st.title("📥 Inbox Monitor")
    st.caption("Detect customer replies and filter out bounce/auto-reply noise")

    conn = get_conn()
    try:
        tab1, tab2 = st.tabs(["📨 Recent Replies", "🔍 Manual Check"])

        with tab1:
            _render_recent_replies(conn)
        with tab2:
            _render_manual_check(conn)
    finally:
        conn.close()


# ── Recent Replies ─────────────────────────────────────────────────────


def _render_recent_replies(conn):
    """Display recent inbound replies detected via IMAP."""
    st.subheader("📨 Recent Customer Replies")

    hours = st.select_slider(
        "Time window",
        options=[1, 6, 12, 24, 48, 72, 168],
        value=24,
        format_func=lambda h: f"Last {h}h" if h < 48 else f"Last {h//24}d",
        key="inbox_hours",
    )

    # Fetch recent inbound communications
    rows = conn.execute(
        """SELECT c.*, l.company_name, l.email as lead_email, l.contact_name
           FROM communications c
           LEFT JOIN leads l ON l.id = c.lead_id
           WHERE c.direction = 'inbound'
             AND c.sent_at >= datetime('now', ?)
           ORDER BY c.sent_at DESC
           LIMIT 50""",
        (f"-{hours} hours",),
    ).fetchall()

    # Also show outbound emails that received replies
    replied = conn.execute(
        """SELECT c.*, l.company_name, l.email as lead_email, l.contact_name
           FROM communications c
           LEFT JOIN leads l ON l.id = c.lead_id
           WHERE c.status = 'replied'
             AND c.replied_at >= datetime('now', ?)
           ORDER BY c.replied_at DESC
           LIMIT 30""",
        (f"-{hours} hours",),
    ).fetchall()

    col1, col2 = st.columns(2)
    col1.metric("Inbound Messages", len(rows))
    col2.metric("Replied Outbound", len(replied))

    if not rows and not replied:
        empty_state("No recent replies detected", "📭")
        return

    # Inbound messages
    if rows:
        st.markdown("#### Inbound Messages")
        for r in rows:
            with st.expander(
                f"📩 From: {r['lead_email'] or 'unknown'} | "
                f"Subject: {(r['subject'] or '(no subject)')[:60]}",
                expanded=False,
            ):
                st.markdown(f"**From:** {r.get('lead_email') or '?'}")
                st.markdown(f"**Company:** {r.get('company_name') or '?'}")
                st.markdown(f"**Subject:** {r.get('subject') or '—'}")
                st.markdown(f"**Status:** {status_badge(r.get('status', 'unknown'))}",
                            unsafe_allow_html=True)
                if r.get("in_reply_to"):
                    st.caption(f"**In-Reply-To:** `{r['in_reply_to']}`")
                if r.get("body"):
                    st.text_area(
                        "Body",
                        value=r["body"][:500],
                        height=120,
                        key=f"body_{r['id']}",
                        disabled=True,
                    )
                st.caption(f"Received: {r.get('sent_at', '?')}")

    # Replied outbound
    if replied:
        st.markdown("#### Outbound Emails That Got Replies")
        reply_data = []
        for r in replied:
            reply_data.append({
                "Company": r.get("company_name") or "?",
                "Email": r.get("lead_email") or "?",
                "Subject": (r.get("subject") or "")[:60],
                "Replied At": (r.get("replied_at") or "")[:16],
            })
        st.dataframe(pd.DataFrame(reply_data), use_container_width=True)


# ── Manual Check ───────────────────────────────────────────────────────


def _render_manual_check(conn):
    """Trigger a manual IMAP inbox check."""
    st.subheader("🔍 Manual Inbox Check")
    st.caption("Connect to IMAP and scan for new replies/bounces")

    # Configuration status
    import config
    has_config = bool(config.IMAP_HOST and config.IMAP_USERNAME and config.IMAP_PASSWORD)

    if not has_config:
        st.warning("⚠️ IMAP not configured. Set IMAP_HOST, IMAP_USERNAME, IMAP_PASSWORD in .env")
        return

    st.success(f"✅ IMAP configured: **{config.IMAP_USERNAME}** @ **{config.IMAP_HOST}:{config.IMAP_PORT}**")

    hours_back = st.slider(
        "Check messages from last N hours",
        1, 72, 24,
        key="manual_check_hours",
    )

    if st.button("🔍 Run Inbox Check", type="primary", key="manual_check_btn"):
        with st.spinner("Connecting to IMAP and scanning inbox..."):
            try:
                from emailer.inbox import InboxMonitor
                monitor = InboxMonitor(conn)
                result = monitor.check_inbox(hours_back=hours_back)
            except Exception as e:
                error_toast(f"IMAP check failed: {e}")
                return

        # Display results
        st.divider()
        st.subheader("📊 Check Results")

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("📨 Checked", result.checked)
        col2.metric("💬 Replies", len(result.replies))
        col3.metric("🔄 Auto-Replies", result.auto_replies)
        col4.metric("📭 Bounces", result.bounces)

        if result.errors:
            for err in result.errors:
                st.warning(f"⚠️ {err}")

        if result.replies:
            st.success(f"🎉 Found {len(result.replies)} customer repl{'y' if len(result.replies) == 1 else 'ies'}!")
            for reply in result.replies[:20]:
                r = reply.to_dict() if hasattr(reply, 'to_dict') else reply
                with st.expander(
                    f"💬 {r.get('from_email', '?')} — {str(r.get('subject', ''))[:60]}",
                    expanded=True,
                ):
                    st.json(r)
        else:
            st.info("No customer replies found in this window.")
