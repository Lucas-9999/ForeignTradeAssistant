"""
Collector page — scraping task status, collection health, and manual triggers.
"""

import streamlit as st
import pandas as pd
from datetime import datetime

from ui.components import get_conn, empty_state, status_badge, success_toast, error_toast


def render_collector():
    """Render the Collector status and control page."""
    st.title("🔍 Collector Monitor")
    st.caption("Scraping task history, collection health, and manual triggers")

    conn = get_conn()
    try:
        tab1, tab2, tab3 = st.tabs(["📊 Status", "🏥 Health", "▶️ Trigger"])

        with tab1:
            _render_task_status(conn)
        with tab2:
            _render_health(conn)
        with tab3:
            _render_trigger()

    finally:
        conn.close()


def _render_task_status(conn):
    """Show recent scraping tasks."""
    st.subheader("📊 Recent Scraping Tasks")

    tasks = conn.execute(
        """SELECT * FROM scraping_tasks
           ORDER BY started_at DESC LIMIT 30"""
    ).fetchall()

    if not tasks:
        empty_state("No scraping tasks recorded yet", "🤖")
        return

    # Summary stats
    total = len(tasks)
    completed = sum(1 for t in tasks if t["status"] == "completed")
    failed = sum(1 for t in tasks if t["status"] == "failed")
    total_found = sum(t["leads_found"] or 0 for t in tasks)
    total_new = sum(t["leads_new"] or 0 for t in tasks)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Runs (recent)", total)
    col2.metric("✅ Completed", completed, delta=f"{completed}/{total}")
    col3.metric("Leads Found", total_found)
    col4.metric("New Leads", total_new)

    st.divider()

    # Task table
    task_rows = []
    for t in tasks:
        duration = _compute_duration(t["started_at"], t["completed_at"])
        task_rows.append({
            "ID": t["id"],
            "Source": t["source"],
            "Status": t["status"],
            "Found": t["leads_found"] or 0,
            "New": t["leads_new"] or 0,
            "Duration": duration,
            "Started": (t["started_at"] or "")[:16],
            "Error": (t["error_message"] or "")[:80],
        })

    st.dataframe(pd.DataFrame(task_rows), use_container_width=True, height=300)

    # Source breakdown
    st.subheader("By Source")
    by_source = conn.execute(
        """SELECT source,
                  COUNT(*) as runs,
                  SUM(leads_found) as total_found,
                  SUM(leads_new) as total_new,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failures
           FROM scraping_tasks
           GROUP BY source"""
    ).fetchall()

    if by_source:
        src_rows = []
        for s in by_source:
            fail_rate = f"{s['failures']}/{s['runs']}" if s["failures"] > 0 else "0"
            src_rows.append({
                "Source": s["source"],
                "Runs": s["runs"],
                "Leads Found": s["total_found"] or 0,
                "New Leads": s["total_new"] or 0,
                "Failures": fail_rate,
            })
        st.dataframe(pd.DataFrame(src_rows), use_container_width=True)


def _render_health(conn):
    """Show collection health metrics."""
    st.subheader("🏥 Collection Health (Last 30 Days)")

    health = conn.execute(
        """SELECT * FROM collection_health
           WHERE started_at >= datetime('now', '-30 days')
           ORDER BY started_at DESC
           LIMIT 50"""
    ).fetchall()

    if not health:
        empty_state("No health data recorded", "💚")
        return

    # Average health score
    avg_score = sum(h["health_score"] or 100 for h in health) / len(health)
    score_color = "🟢" if avg_score >= 80 else "🟡" if avg_score >= 50 else "🔴"

    col1, col2, col3 = st.columns(3)
    col1.metric("Avg Health Score", f"{score_color} {avg_score:.0f}%")
    col2.metric("Runs (30d)", len(health))
    col3.metric("Errors", sum(h["error_count"] or 0 for h in health))

    # Health table
    health_rows = []
    for h in health:
        health_rows.append({
            "Source": h["source"],
            "Started": (h["started_at"] or "")[:16],
            "Score": f"{h['health_score'] or 100:.0f}%",
            "Leads Found": h["leads_found"] or 0,
            "New": h["leads_new"] or 0,
            "Requests": h["total_requests"] or 0,
            "Errors": h["error_count"] or 0,
            "Rate Limits": h["rate_limit_count"] or 0,
            "CB Hits": h["circuit_breaker_hits"] or 0,
        })
    st.dataframe(pd.DataFrame(health_rows), use_container_width=True, height=300)

    # Country coverage
    st.subheader("🌍 Country Rotation")

    segments = conn.execute(
        """SELECT * FROM collection_segments
           ORDER BY last_collected_at DESC"""
    ).fetchall()

    if segments:
        seg_rows = []
        for s in segments:
            seg_rows.append({
                "Country": s["country_key"],
                "Last Collected": (s["last_collected_at"] or "never")[:16],
                "Count": s["collection_count"] or 0,
                "Last Found": s["last_leads_found"] or 0,
                "Errors": s["consecutive_errors"] or 0,
            })
        st.dataframe(pd.DataFrame(seg_rows), use_container_width=True)


def _render_trigger():
    """Manual collection trigger."""
    st.subheader("▶️ Manual Collection Trigger")
    st.caption("Start a collection run for a specific source")

    source = st.selectbox(
        "Source",
        ["facebook", "industry_site", "xunkexing"],
        key="collector_source",
    )

    dry_run = st.checkbox("Dry run (don't save leads)", key="collector_dry_run")

    if st.button("🚀 Start Collection", type="primary", key="start_collection", use_container_width=True):
        with st.spinner(f"Starting {source} collection..."):
            try:
                import subprocess
                import sys
                from pathlib import Path

                project_root = Path(__file__).resolve().parent.parent
                cmd = [
                    sys.executable, "-m", "collector.cli", "run",
                    "--source", source,
                ]
                if dry_run:
                    cmd.append("--dry-run")

                # Run in background
                subprocess.Popen(
                    cmd,
                    cwd=str(project_root),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                success_toast(f"Collection started for {source}. Check Status tab for progress.")
            except Exception as e:
                error_toast(f"Failed to start collection: {e}")


def _compute_duration(started_at: str, completed_at: str) -> str:
    """Compute human-readable duration between two ISO timestamps."""
    if not started_at or not completed_at:
        return "—"
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        secs = (end - start).total_seconds()
        if secs < 60:
            return f"{secs:.0f}s"
        elif secs < 3600:
            return f"{secs/60:.1f}m"
        else:
            return f"{secs/3600:.1f}h"
    except Exception:
        return "—"
