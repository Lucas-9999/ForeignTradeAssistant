"""
Shared UI components — reusable widgets across all Streamlit pages.
"""

import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime
from typing import Optional


def get_conn() -> sqlite3.Connection:
    """Get a database connection with WAL mode and foreign keys."""
    import config
    from crm.db import get_connection as _get_conn
    return _get_conn(config.DB_PATH)


def status_badge(status: str) -> str:
    """Return a colored HTML badge for lead/email status."""
    colors = {
        "new": "#3498db",
        "contacted": "#f39c12",
        "replied": "#2ecc71",
        "customer": "#9b59b6",
        "dead": "#95a5a6",
        "sent": "#2ecc71",
        "failed": "#e74c3c",
        "bounced": "#e74c3c",
        "pending": "#f39c12",
        "draft": "#95a5a6",
        "running": "#3498db",
        "completed": "#2ecc71",
    }
    color = colors.get(status, "#95a5a6")
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:10px;font-size:0.8em">{status}</span>'


def metric_card(label: str, value, delta=None, help_text: str = None):
    """Render a styled metric card."""
    st.metric(label=label, value=value, delta=delta, help=help_text)


def confirm_action(label: str, message: str, key: str) -> bool:
    """Show a confirmation button that requires a second click.

    Returns True only on the second click.
    """
    confirm_key = f"confirm_{key}"
    if confirm_key not in st.session_state:
        st.session_state[confirm_key] = False

    if not st.session_state[confirm_key]:
        if st.button(f"⚠️ {label}", key=f"btn_{key}", type="secondary"):
            st.session_state[confirm_key] = True
            st.rerun()
        return False
    else:
        col1, col2 = st.columns(2)
        with col1:
            if st.button(f"✅ Confirm {label}", key=f"yes_{key}", type="primary"):
                st.session_state[confirm_key] = False
                return True
        with col2:
            if st.button("❌ Cancel", key=f"no_{key}"):
                st.session_state[confirm_key] = False
                st.rerun()
        return False


def build_lead_dataframe(leads: list) -> pd.DataFrame:
    """Convert Lead objects to a display-ready DataFrame with columns renamed."""
    if not leads:
        return pd.DataFrame()
    df = pd.DataFrame([l.to_dict() for l in leads])
    col_map = {
        "id": "ID",
        "company_name": "Company",
        "contact_name": "Contact",
        "email": "Email",
        "phone": "Phone",
        "country": "Country",
        "industry": "Industry",
        "source": "Source",
        "status": "Status",
        "discovered_at": "Discovered",
        "first_contacted_at": "First Contacted",
    }
    display_cols = [c for c in col_map if c in df.columns]
    df = df[display_cols].rename(columns=col_map)
    return df


def render_lead_card(lead_dict: dict):
    """Render a single lead as an expandable card."""
    with st.expander(f"#{lead_dict['id']} {lead_dict.get('company_name') or 'Unknown'}", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            st.markdown(f"**Contact:** {lead_dict.get('contact_name') or '—'}")
            st.markdown(f"**Email:** {lead_dict.get('email') or '—'}")
            st.markdown(f"**Phone:** {lead_dict.get('phone') or '—'}")
            st.markdown(f"**WhatsApp:** {lead_dict.get('whatsapp') or '—'}")
        with col2:
            st.markdown(f"**Country:** {lead_dict.get('country') or '—'}")
            st.markdown(f"**Industry:** {lead_dict.get('industry') or '—'}")
            st.markdown(f"**Source:** {lead_dict.get('source') or '—'}")
            st.markdown(f"**Status:** {status_badge(lead_dict.get('status', 'new'))}", unsafe_allow_html=True)
        if lead_dict.get("website"):
            st.markdown(f"**Website:** {lead_dict['website']}")
        if lead_dict.get("notes"):
            st.markdown(f"**Notes:** {lead_dict['notes']}")


def empty_state(message: str = "No data found", icon: str = "📭"):
    """Render a centered empty-state placeholder."""
    st.markdown(
        f"<div style='text-align:center;padding:3rem 0;color:#888'>{icon}<br>{message}</div>",
        unsafe_allow_html=True,
    )


def section_header(title: str, description: str = ""):
    """Render a styled section header."""
    st.markdown(f"### {title}")
    if description:
        st.caption(description)


def error_toast(message: str):
    """Show an error as a toast + inline message."""
    st.error(message)


def success_toast(message: str):
    """Show a success message."""
    st.success(message)
