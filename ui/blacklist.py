"""
Blacklist page — manage never-send email addresses.
"""

import streamlit as st
import pandas as pd

from crm.manager import BlacklistManager
from ui.components import get_conn, empty_state, success_toast, error_toast


def render_blacklist():
    """Render the Blacklist management page."""
    st.title("🚫 Blacklist")
    st.caption("Emails that should never receive outreach (bounces, unsubscribes, complaints)")

    conn = get_conn()
    try:
        bl_mgr = BlacklistManager(conn)

        # ── Add to blacklist ──────────────────────────────────────────
        with st.expander("➕ Add Entry", expanded=False):
            with st.form("add_blacklist_form"):
                col1, col2 = st.columns(2)
                with col1:
                    email = st.text_input("Email address *")
                with col2:
                    reason = st.selectbox(
                        "Reason",
                        ["bounced", "unsubscribed", "complaint", "invalid"],
                    )

                if st.form_submit_button("Add to Blacklist", type="primary"):
                    if not email:
                        error_toast("Email is required")
                    elif "@" not in email:
                        error_toast("Invalid email format")
                    else:
                        bl_mgr.add(email, reason)
                        success_toast(f"Added {email} to blacklist")
                        st.rerun()

        # ── Blacklist table ───────────────────────────────────────────
        st.divider()
        entries = bl_mgr.list_all()

        if not entries:
            empty_state("Blacklist is empty — no bounced or unsubscribed emails", "✅")
            return

        st.subheader(f"Blocked Emails ({len(entries)})")

        df_rows = []
        for e in entries:
            df_rows.append({
                "ID": e.id,
                "Email": e.email,
                "Reason": e.reason,
                "Added": (e.created_at or "")[:16],
            })

        df = pd.DataFrame(df_rows)
        st.dataframe(df, use_container_width=True, height=400)

        # ── Remove from blacklist ────────────────────────────────────
        st.divider()
        st.subheader("Remove Entry")
        col1, col2 = st.columns([3, 1])
        with col1:
            email_remove = st.text_input(
                "Email to remove",
                placeholder="e.g. bounced@example.com",
                key="bl_remove_input",
            )
        with col2:
            if st.button("✅ Remove", key="bl_remove_btn", use_container_width=True):
                if email_remove:
                    bl_mgr.remove(email_remove)
                    success_toast(f"Removed {email_remove} from blacklist")
                    st.rerun()

        # ── Export ────────────────────────────────────────────────────
        st.divider()
        if st.button("📥 Export CSV", key="bl_export", use_container_width=True):
            csv = df.to_csv(index=False)
            st.download_button(
                label="Download Blacklist CSV",
                data=csv,
                file_name="blacklist_export.csv",
                mime="text/csv",
            )

    finally:
        conn.close()
