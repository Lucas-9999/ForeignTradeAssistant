"""
Templates page — view, add, edit, and toggle email templates.
"""

import streamlit as st

from crm.manager import TemplateManager
from crm.models import EmailTemplate
from emailer.templates import EmailTemplateRenderer
from ui.components import get_conn, empty_state, success_toast, error_toast, confirm_action


def render_templates():
    """Render the Email Templates management page."""
    st.title("📋 Email Templates")

    conn = get_conn()
    try:
        tmpl_mgr = TemplateManager(conn)
        renderer = EmailTemplateRenderer()

        tab1, tab2 = st.tabs(["📜 Browse Templates", "➕ Add Template"])

        # ═══════════════════════════════════════════════════════════
        # TAB 1: Browse + Edit Templates
        # ═══════════════════════════════════════════════════════════
        with tab1:
            templates = tmpl_mgr.list_active()
            all_templates = conn.execute(
                "SELECT * FROM email_templates ORDER BY category, name"
            ).fetchall()

            if not all_templates:
                empty_state("No templates found")
                return

            # Category summary
            categories = {}
            for t in all_templates:
                cat = t["category"]
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(t)

            st.subheader(f"Templates by Category ({len(all_templates)} total)")

            for cat, cat_templates in categories.items():
                with st.expander(f"📁 {cat.upper()} ({len(cat_templates)} templates)", expanded=False):
                    for t in cat_templates:
                        cols = st.columns([3, 1, 1, 1])
                        with cols[0]:
                            st.markdown(
                                f"**{t['name']}**  "
                                f"{'🟢' if t['is_active'] else '🔴'}"
                            )
                            st.caption(
                                f"Used {t['usage_count']}× | "
                                f"Last: {str(t['last_used_at'] or 'never')[:16]} | "
                                f"ID: {t['id']}"
                            )
                        with cols[1]:
                            if st.button("👁️ Preview", key=f"prev_{t['id']}"):
                                st.session_state[f"preview_tmpl_{t['id']}"] = True
                        with cols[2]:
                            new_status = 0 if t["is_active"] else 1
                            label = "🔴 Deactivate" if t["is_active"] else "🟢 Activate"
                            if st.button(label, key=f"toggle_{t['id']}"):
                                tmpl = tmpl_mgr.get_by_id(t["id"])
                                if tmpl:
                                    tmpl.is_active = new_status
                                    tmpl_mgr.update(tmpl)
                                    success_toast(f"Template '{t['name']}' {'deactivated' if t['is_active'] else 'activated'}")
                                    st.rerun()
                        with cols[3]:
                            if st.button("🗑️", key=f"del_tmpl_{t['id']}", help="Delete template"):
                                if confirm_action(f"Delete '{t['name']}'",
                                                   "This is permanent!", f"deltmpl_{t['id']}"):
                                    tmpl_mgr.delete(t["id"])
                                    success_toast(f"Deleted '{t['name']}'")
                                    st.rerun()

                        # Show preview if requested
                        if st.session_state.get(f"preview_tmpl_{t['id']}"):
                            st.divider()
                            st.markdown(f"#### Preview: {t['name']}")
                            preview = renderer.preview(t["subject_template"], t["body_template"])
                            st.text_input("Subject", value=preview["subject"],
                                          key=f"subj_prev_{t['id']}", disabled=True)
                            st.html(preview["body"])
                            st.markdown(f"**Description:** {t['description'] or '—'}")
                            st.markdown(f"**Subject template:** `{t['subject_template']}`")
                            if st.button("Close preview", key=f"close_prev_{t['id']}"):
                                st.session_state[f"preview_tmpl_{t['id']}"] = False
                                st.rerun()

                        # Edit form
                        with st.expander(f"✏️ Edit '{t['name']}'", expanded=False):
                            with st.form(f"edit_tmpl_form_{t['id']}"):
                                edit_name = st.text_input("Name", value=t["name"])
                                edit_subject = st.text_input("Subject Template", value=t["subject_template"])
                                edit_body = st.text_area("Body Template (HTML)", value=t["body_template"],
                                                         height=200)
                                edit_desc = st.text_input("Description", value=t["description"] or "")
                                edit_cat = st.selectbox(
                                    "Category",
                                    ["icebreaker", "followup", "general", "custom", "promotional", "seasonal"],
                                    index=["icebreaker", "followup", "general", "custom", "promotional",
                                           "seasonal"].index(t["category"])
                                    if t["category"] in ["icebreaker", "followup", "general", "custom",
                                                         "promotional", "seasonal"] else 0,
                                )

                                if st.form_submit_button("💾 Save"):
                                    tmpl = tmpl_mgr.get_by_id(t["id"])
                                    if tmpl:
                                        tmpl.name = edit_name
                                        tmpl.subject_template = edit_subject
                                        tmpl.body_template = edit_body
                                        tmpl.description = edit_desc or None
                                        tmpl.category = edit_cat
                                        tmpl_mgr.update(tmpl)
                                        success_toast(f"Updated '{edit_name}'")
                                        st.rerun()

        # ═══════════════════════════════════════════════════════════
        # TAB 2: Add Template
        # ═══════════════════════════════════════════════════════════
        with tab2:
            st.subheader("➕ Add New Template")
            with st.form("add_template_form"):
                col1, col2 = st.columns(2)
                with col1:
                    name = st.text_input("Template Name *")
                    subject = st.text_input("Subject Template *",
                                            placeholder="e.g. Quick question about {{ company_name }}")
                    category = st.selectbox("Category", ["icebreaker", "followup", "general", "custom",
                                                         "promotional", "seasonal"])
                with col2:
                    language = st.selectbox("Language", ["en", "zh", "es", "de", "fr"])
                    desc = st.text_input("Description")

                body = st.text_area(
                    "Body Template (HTML) *",
                    height=250,
                    placeholder="<p>Dear {{ contact_name | default('Sir/Madam') }},</p>..."
                )

                st.caption(
                    "Variables: `{{ company_name }}` `{{ contact_name }}` `{{ country }}` "
                    "`{{ industry }}` `{{ source }}`"
                )

                if st.form_submit_button("➕ Add Template", type="primary"):
                    if not name or not subject or not body:
                        error_toast("Name, subject, and body are required")
                    else:
                        tmpl = EmailTemplate(
                            name=name,
                            subject_template=subject,
                            body_template=body,
                            description=desc or None,
                            category=category,
                            language=language,
                        )
                        tmpl = tmpl_mgr.create(tmpl)
                        success_toast(f"Template '{name}' created!")
                        st.rerun()

    finally:
        conn.close()
