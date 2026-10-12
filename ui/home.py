"""
Dashboard page — KPIs, charts, and collection health overview.
"""

import streamlit as st
import pandas as pd

from crm.manager import LeadManager, CustomerManager, CommunicationManager
from emailer.rotation import AntiSpamGuard
from ui.components import get_conn, metric_card, section_header


def render_dashboard():
    """Render the FTA Dashboard with KPIs and charts."""
    st.title("📊 Dashboard")

    conn = get_conn()
    try:
        lead_mgr = LeadManager(conn)
        comm_mgr = CommunicationManager(conn)
        cust_mgr = CustomerManager(conn)
        guard = AntiSpamGuard(conn)

        status_counts = lead_mgr.count_by_status()
        quota = guard.get_status()

        # ── Row 1: Lead KPIs ──────────────────────────────────────────
        st.subheader("📋 Lead Pipeline")
        col1, col2, col3, col4, col5 = st.columns(5)
        total = sum(status_counts.values())
        col1.metric("Total Leads", total)
        col2.metric("🆕 New", status_counts.get("new", 0))
        col3.metric("📨 Contacted", status_counts.get("contacted", 0))
        col4.metric("💬 Replied", status_counts.get("replied", 0))
        col5.metric("✅ Customers", status_counts.get("customer", 0))

        # ── Conversion funnel chart ───────────────────────────────────
        st.divider()
        col_chart, col_spam = st.columns([2, 1])

        with col_chart:
            st.subheader("Conversion Funnel")
            funnel_data = {
                "Stage": ["New", "Contacted", "Replied", "Customer"],
                "Count": [
                    status_counts.get("new", 0),
                    status_counts.get("contacted", 0),
                    status_counts.get("replied", 0),
                    status_counts.get("customer", 0),
                ],
            }
            funnel_df = pd.DataFrame(funnel_data).set_index("Stage")
            st.bar_chart(funnel_df, horizontal=True, height=200)

        with col_spam:
            st.subheader("📧 Sending Quota")
            st.metric("Sent Today", quota["emails_sent_today"],
                      delta=f"Limit: {quota['daily_limit']}")
            st.metric("Remaining", quota["remaining"])
            if quota["domain_counts"]:
                st.caption("**Domains contacted today:**")
                for domain, count in sorted(quota["domain_counts"].items(), key=lambda x: -x[1]):
                    st.text(f"  {domain}: {count}")

        # ── Row 2: Recent activity + Overdue follow-ups ──────────────
        st.divider()
        col_left, col_right = st.columns(2)

        with col_left:
            st.subheader("🕐 Latest Outbound Emails")
            recent = conn.execute(
                """SELECT c.id, l.company_name, c.subject, c.status, c.sent_at
                   FROM communications c
                   LEFT JOIN leads l ON l.id = c.lead_id
                   WHERE c.direction = 'outbound'
                   ORDER BY c.sent_at DESC LIMIT 5"""
            ).fetchall()
            if recent:
                for r in recent:
                    ts = r["sent_at"][:16] if r["sent_at"] else "?"
                    status_icon = "✅" if r["status"] == "sent" else "❌"
                    st.caption(
                        f"{status_icon} **{r['company_name']}** — "
                        f"_'{str(r['subject'])[:60] if r['subject'] else '—'}'_ ({ts})"
                    )
            else:
                st.caption("No emails sent yet.")

        with col_right:
            st.subheader("⚠️ Overdue Follow-ups")
            overdue = cust_mgr.get_overdue_followups()
            if overdue:
                for c in overdue[:8]:
                    st.warning(
                        f"**{c.company_name}** — due {c.next_followup_at or '?'}"
                    )
            else:
                st.success("All follow-ups up to date!")

        # ── Row 3: Collection health ──────────────────────────────────
        st.divider()
        st.subheader("🔍 Collection Health")

        # Recent scraping tasks
        tasks = conn.execute(
            """SELECT source, status, leads_found, leads_new, error_message,
                      started_at, completed_at
               FROM scraping_tasks
               ORDER BY started_at DESC LIMIT 10"""
        ).fetchall()

        if tasks:
            task_rows = []
            for t in tasks:
                duration = ""
                if t["started_at"] and t["completed_at"]:
                    # Compute duration from ISO timestamps
                    try:
                        from datetime import datetime as dt
                        start = dt.fromisoformat(t["started_at"].replace("Z", "+00:00"))
                        end = dt.fromisoformat(t["completed_at"].replace("Z", "+00:00"))
                        secs = (end - start).total_seconds()
                        if secs >= 60:
                            duration = f"{secs/60:.1f}m"
                        else:
                            duration = f"{secs:.0f}s"
                    except Exception:
                        pass
                status_icon = {
                    "completed": "✅", "running": "🔄", "failed": "❌",
                }.get(t["status"], "❓")
                task_rows.append({
                    "Source": t["source"],
                    "Status": f"{status_icon} {t['status']}",
                    "Found": t["leads_found"] or 0,
                    "New": t["leads_new"] or 0,
                    "Duration": duration,
                    "Error": (t["error_message"] or "")[:60],
                })
            st.dataframe(pd.DataFrame(task_rows), use_container_width=True, height=200)

        # Collection health stats
        health = conn.execute(
            """SELECT source, COUNT(*) as runs,
                      SUM(leads_found) as total_found,
                      SUM(leads_new) as total_new,
                      ROUND(AVG(health_score), 1) as avg_health
               FROM collection_health
               WHERE started_at >= datetime('now', '-30 days')
               GROUP BY source"""
        ).fetchall()

        if health:
            health_rows = []
            for h in health:
                health_rows.append({
                    "Source": h["source"],
                    "Runs (30d)": h["runs"],
                    "Leads Found": h["total_found"] or 0,
                    "New Leads": h["total_new"] or 0,
                    "Avg Health": f"{h['avg_health'] or 100:.0f}%",
                })
            st.caption("Last 30 days")
            st.dataframe(pd.DataFrame(health_rows), use_container_width=True, height=150)

    finally:
        conn.close()
