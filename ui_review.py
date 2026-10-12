"""
Streamlit Review UI — internal tool for lead management and email operations.

Multi-file architecture: each page is a module under ui/ that exports render_*().

Usage:
    streamlit run ui_review.py
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st

from crm.db import build_database
import config

# ── Page config ──────────────────────────────────────────────────────────

st.set_page_config(
    page_title="FTA — Foreign Trade Assistant",
    page_icon="📧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Init ─────────────────────────────────────────────────────────────────

DB_PATH = config.DB_PATH
if not DB_PATH.exists():
    build_database(DB_PATH)

# ── Sidebar Navigation ───────────────────────────────────────────────────

st.sidebar.title("📧 FTA Control Panel")
st.sidebar.caption("Foreign Trade Assistant v2")

page = st.sidebar.radio(
    "Navigation",
    [
        "📊 Dashboard",
        "👤 Leads",
        "🏢 Customers",
        "📧 Send Email",
        "📥 Inbox",
        "📋 Templates",
        "🔍 Collector",
        "🚫 Blacklist",
    ],
)

# ── Quick stats in sidebar ───────────────────────────────────────────────

with st.sidebar:
    st.divider()
    try:
        from crm.db import get_connection
        conn = get_connection(DB_PATH)
        new_count = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE status='new'"
        ).fetchone()[0]
        replied_count = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE status='replied'"
        ).fetchone()[0]
        total_count = conn.execute(
            "SELECT COUNT(*) FROM leads"
        ).fetchone()[0]
        conn.close()

        col_a, col_b, col_c = st.columns(3)
        col_a.metric("New", new_count)
        col_b.metric("Replied", replied_count)
        col_c.metric("Total", total_count)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════════════
# Page Router — each block imports and calls the relevant render function
# ═══════════════════════════════════════════════════════════════════════════

if page == "📊 Dashboard":
    from ui.home import render_dashboard
    render_dashboard()

elif page == "👤 Leads":
    from ui.leads import render_leads
    render_leads()

elif page == "🏢 Customers":
    from ui.customers import render_customers
    render_customers()

elif page == "📧 Send Email":
    from ui.email_send import render_email_send
    render_email_send()

elif page == "📥 Inbox":
    from ui.inbox import render_inbox
    render_inbox()

elif page == "📋 Templates":
    from ui.templates import render_templates
    render_templates()

elif page == "🔍 Collector":
    from ui.collector import render_collector
    render_collector()

elif page == "🚫 Blacklist":
    from ui.blacklist import render_blacklist
    render_blacklist()
