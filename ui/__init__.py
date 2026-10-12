"""
UI module — Streamlit pages for Foreign Trade Assistant.

Each module exports a render_*() function called from ui_review.py.
"""

from ui.home import render_dashboard
from ui.leads import render_leads
from ui.customers import render_customers
from ui.email_send import render_email_send
from ui.inbox import render_inbox
from ui.templates import render_templates
from ui.collector import render_collector
from ui.blacklist import render_blacklist

__all__ = [
    "render_dashboard",
    "render_leads",
    "render_customers",
    "render_email_send",
    "render_inbox",
    "render_templates",
    "render_collector",
    "render_blacklist",
]
