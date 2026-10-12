"""
Collection Dashboard — FastAPI-rendered HTML monitoring interface.

Provides:
  - /dashboard/            — Main overview (health, recent runs, country rotation)
  - /dashboard/health      — Health history with trend visualization
  - /dashboard/segments    — Country rotation schedule detail
  - /api/v1/dashboard/data — JSON endpoint for all dashboard data

Design: pure HTML + inline CSS + minimal JS. No external dependencies.
Auto-refreshes every 60 seconds.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, date
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

import config
from crm.db import build_database, get_connection

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# ═══════════════════════════════════════════════════════════════════════════
# CSS (shared across pages)
# ═══════════════════════════════════════════════════════════════════════════

DASHBOARD_CSS = """
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f5f7fa; color: #1a1a2e; padding: 24px; }
  h1 { font-size: 24px; margin-bottom: 4px; }
  .subtitle { color: #666; font-size: 14px; margin-bottom: 24px; }
  .nav { display: flex; gap: 12px; margin-bottom: 24px; }
  .nav a { padding: 8px 16px; background: #fff; border-radius: 6px; text-decoration: none;
           color: #1a1a2e; font-weight: 500; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  .nav a.active { background: #4361ee; color: #fff; }
  .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
             gap: 16px; margin-bottom: 24px; }
  .card { background: #fff; border-radius: 10px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,.06); }
  .card .label { font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: .5px; }
  .card .value { font-size: 32px; font-weight: 700; margin: 8px 0; }
  .card .sub { font-size: 13px; color: #999; }
  .health-good { color: #2ecc71; }
  .health-warn { color: #f39c12; }
  .health-bad { color: #e74c3c; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px;
          overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,.06); margin-bottom: 24px; }
  th { background: #f8f9fb; padding: 12px 16px; text-align: left; font-size: 12px;
       color: #888; text-transform: uppercase; letter-spacing: .5px; }
  td { padding: 12px 16px; font-size: 14px; border-top: 1px solid #f0f0f0; }
  tr:hover td { background: #fafbfc; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 600; }
  .badge-ok { background: #e8f5e9; color: #2e7d32; }
  .badge-warn { background: #fff3e0; color: #e65100; }
  .badge-bad { background: #ffebee; color: #c62828; }
  .bar-chart { display: flex; align-items: flex-end; gap: 4px; height: 120px; padding: 8px 0; }
  .bar-chart .bar { flex: 1; border-radius: 3px 3px 0 0; min-width: 12px;
                    transition: height .3s; position: relative; }
  .bar-chart .bar:hover { opacity: .8; }
  .bar-chart .bar .tip { display: none; position: absolute; bottom: 100%; left: 50%;
                          transform: translateX(-50%); background: #333; color: #fff;
                          padding: 4px 8px; border-radius: 4px; font-size: 11px; white-space: nowrap; }
  .bar-chart .bar:hover .tip { display: block; }
  .refresh { font-size: 12px; color: #aaa; text-align: right; margin-bottom: 12px; }
  .section-title { font-size: 16px; font-weight: 600; margin: 24px 0 12px; color: #444; }
  .empty-state { text-align: center; padding: 48px 24px; color: #999; }
  .progress-bar { height: 8px; background: #e9ecef; border-radius: 4px; overflow: hidden; margin-top: 8px; }
  .progress-bar .fill { height: 100%; border-radius: 4px; transition: width .5s; }
</style>
"""

# ═══════════════════════════════════════════════════════════════════════════
# Data helpers
# ═══════════════════════════════════════════════════════════════════════════


def _get_db():
    """Get a DB connection, building it if needed."""
    if not config.DB_PATH.exists():
        build_database(config.DB_PATH)
    return get_connection(config.DB_PATH)


def _health_class(score: Optional[float]) -> str:
    """CSS class for health score."""
    if score is None:
        return ""
    if score >= 80:
        return "health-good"
    elif score >= 50:
        return "health-warn"
    return "health-bad"


def _health_badge(score: Optional[float]) -> str:
    """Badge HTML for health score."""
    if score is None:
        return '<span class="badge">N/A</span>'
    if score >= 80:
        return f'<span class="badge badge-ok">{score:.0f}/100</span>'
    elif score >= 50:
        return f'<span class="badge badge-warn">{score:.0f}/100</span>'
    return f'<span class="badge badge-bad">{score:.0f}/100</span>'


def _get_dashboard_data() -> dict:
    """Collect all dashboard data from the database."""
    conn = _get_db()
    try:
        data = {
            "db_exists": True,
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Total leads in CRM
        lead_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        new_leads = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE status='new'"
        ).fetchone()[0]
        data["total_leads"] = lead_count
        data["new_leads"] = new_leads

        # Today's newly discovered leads (direct from leads table)
        today = date.today().isoformat()
        today_new = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE date(discovered_at) = ?",
            (today,),
        ).fetchone()[0]
        data["today_discovered"] = today_new

        # Latest health record
        health_row = conn.execute(
            """SELECT * FROM collection_health ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
        if health_row:
            data["latest_health"] = dict(health_row)
        else:
            data["latest_health"] = None

        # Health trend (last 7 days)
        from collector.monitor import get_health_trend
        data["health_trend"] = get_health_trend(conn, days=7)

        # Recent tasks
        from collector.scheduler import get_recent_tasks
        data["recent_tasks"] = get_recent_tasks(days=7, limit=20)

        # Collection segments
        seg_rows = conn.execute(
            """SELECT * FROM collection_segments
               ORDER BY last_collected_at ASC NULLS FIRST"""
        ).fetchall()
        data["segments"] = [dict(r) for r in seg_rows]
        data["segment_count"] = len(seg_rows)

        # Email total from communications (ground truth)
        total_sent = conn.execute(
            "SELECT COUNT(*) FROM communications WHERE status='sent'"
        ).fetchone()[0]
        data["total_emails_sent"] = total_sent

        # Scheduler status
        from collector.scheduler import get_scheduler_status
        data["scheduler"] = get_scheduler_status()

        return data
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# API endpoint
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/api/data")
async def dashboard_data():
    """JSON endpoint returning all dashboard data."""
    return _get_dashboard_data()


# ═══════════════════════════════════════════════════════════════════════════
# Page: Main Overview
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/", response_class=HTMLResponse)
async def dashboard_overview():
    """Main dashboard — summary cards, recent runs, country rotation."""
    data = _get_dashboard_data()

    health = data["latest_health"]
    sched = data["scheduler"]
    health_score = health["health_score"] if health else None

    # Build health score card
    score_class = _health_class(health_score)
    score_display = f"{health_score:.0f}" if health_score is not None else "--"

    # Today's leads (direct query on leads table, not cached from health)
    today_leads = data.get("today_discovered", 0)

    # Next run time
    next_run = sched.get("next_run", {})
    next_run_str = next_run.get("scheduled_at", "Not scheduled") if next_run else "Not scheduled"
    next_run_time = ""
    if next_run and next_run.get("hour") is not None:
        next_run_time = f"{next_run['hour']:02d}:{next_run['minute']:02d}"

    # Segment info
    seg_size = sched.get("segmentation", {}).get("segment_size", config.COLLECTION_SEGMENT_SIZE)
    seg_collected = data.get("segment_count", 0)

    # Build health trend bars
    trend_html = ""
    trend = data.get("health_trend", [])
    if trend:
        max_score = max(t.get("avg_score", 0) for t in trend) or 100
        for t in trend:
            score = t.get("avg_score", 0)
            h = max(4, int(score / max_score * 100))
            color = "#2ecc71" if score >= 80 else ("#f39c12" if score >= 50 else "#e74c3c")
            day_label = t.get("day", "")[-5:]  # MM-DD
            trend_html += (
                f'<div class="bar" style="height:{h}%;background:{color}">'
                f'<span class="tip">{day_label}: {score:.0f}/100, {t.get("leads", 0)} leads</span>'
                f'</div>'
            )

    # Build recent tasks table
    tasks_html = ""
    for task in data.get("recent_tasks", [])[:15]:
        status = task.get("status", "?")
        badge_class = "badge-ok" if status == "completed" else "badge-bad"
        started = (task.get("started_at") or "")[:16]
        tasks_html += (
            f"<tr>"
            f"<td>{started}</td>"
            f"<td>{task.get('source', '?')}</td>"
            f"<td><span class='badge {badge_class}'>{status}</span></td>"
            f"<td>{task.get('leads_found', 0)}</td>"
            f"<td>{task.get('leads_new', 0)}</td>"
            f"<td style='font-size:12px;color:#999;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{task.get('error_message') or '-'}</td>"
            f"</tr>"
        )

    # Build segment status table
    seg_html = ""
    country_names = {
        "united_states": "USA", "germany": "Germany", "united_kingdom": "UK",
        "france": "France", "italy": "Italy", "spain": "Spain",
        "netherlands": "Netherlands", "poland": "Poland", "canada": "Canada",
        "australia": "Australia", "japan": "Japan", "south_korea": "South Korea",
        "brazil": "Brazil", "mexico": "Mexico", "turkey": "Turkey",
        "india": "India", "vietnam": "Vietnam", "thailand": "Thailand",
        "indonesia": "Indonesia", "malaysia": "Malaysia",
        "united_arab_emirates": "UAE", "saudi_arabia": "Saudi Arabia",
        "south_africa": "South Africa", "russia": "Russia",
    }
    for seg in data.get("segments", []):
        ck = seg.get("country_key", "")
        name = country_names.get(ck, ck)
        last = (seg.get("last_collected_at") or "Never")[:10]
        errors = seg.get("consecutive_errors", 0)
        err_badge = ""
        if errors > 0:
            err_badge = f' <span class="badge badge-warn">{errors} err</span>'
        seg_html += (
            f"<tr>"
            f"<td>{name}</td>"
            f"<td style='font-size:12px;color:#888'>{ck}</td>"
            f"<td>{last}</td>"
            f"<td>{seg.get('collection_count', 0)}</td>"
            f"<td>{seg.get('last_leads_found', 0)}</td>"
            f"<td>{err_badge if err_badge else '-'}</td>"
            f"</tr>"
        )

    # ── Email batch live status ─────────────────────────────────────────
    email_batch = sched.get("email_batch", {})
    email_enabled = email_batch.get("enabled", False)
    email_sent_today = email_batch.get("sent_today", 0)
    email_sent_total = email_batch.get("sent_total", 0)
    email_remaining = email_batch.get("remaining", config.DAILY_EMAIL_LIMIT)
    email_live = email_batch.get("live", {})
    email_running = email_live.get("running", False)
    email_total = email_live.get("total", 0)
    email_done = email_live.get("sent", 0)
    email_errs = email_live.get("errors", 0)
    email_skipped = email_live.get("skipped", 0)
    email_current = email_live.get("current_company", "")
    email_subject = email_live.get("subject", "")
    email_last_update = email_live.get("last_update", "")

    # Build email card HTML
    email_card_html = ""
    if email_enabled:
        if email_running:
            done_so_far = email_done + email_errs + email_skipped
            pct = int(done_so_far / max(email_total, 1) * 100)
            remaining_in_batch = email_total - done_so_far
            eta = ""
            if email_done > 0 and remaining_in_batch > 0:
                avg_sec = 40
                from datetime import datetime as _dt, timedelta as _td
                eta_dt = _dt.now() + _td(seconds=remaining_in_batch * avg_sec)
                eta = eta_dt.strftime("%H:%M")
            email_card_html = f"""\
  <div class="card" style="grid-column: 1 / -1; border-left: 4px solid #4361ee;">
    <div class="label"><span class="live-indicator"></span> Email Batch — SENDING</div>
    <div class="progress-bar" style="height:16px;margin:12px 0;">
      <div class="fill" style="width:{pct}%;background:linear-gradient(90deg,#4361ee,#7209b7);transition:width 2s"></div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:13px;color:#666;margin-bottom:4px;">
      <span>Sent Today: <strong style="color:#2ecc71">{email_done}</strong></span>
      <span>Errors: <strong style="color:#e74c3c">{email_errs}</strong></span>
      <span>Skipped: <strong style="color:#f39c12">{email_skipped}</strong></span>
      <span>Remaining: <strong>{remaining_in_batch}</strong></span>
      <span>Total: <strong>{email_total}</strong></span>
      <span>{pct}%</span>
    </div>
    <div style="font-size:12px;color:#999;margin-top:6px;">
      Now: {email_current}<br>
      Subj: {email_subject[:80]}{'…' if len(email_subject) > 80 else ''}<br>
      <span style="font-size:11px;">Updated: {email_last_update or ''} | ETA: {eta or '...'} | All-time total sent: <strong>{email_sent_total}</strong></span>
    </div>
  </div>"""
        else:
            daily_pct = min(100, int(email_sent_today / max(config.DAILY_EMAIL_LIMIT, 1) * 100))
            email_card_html = f"""\
  <div class="card">
    <div class="label">Emails Today</div>
    <div class="value">{email_sent_today}<span style="font-size:18px;color:#888">/{config.DAILY_EMAIL_LIMIT}</span></div>
    <div class="sub">{email_remaining} remaining today | <strong>{email_sent_total}</strong> total sent all-time</div>
    <div class="progress-bar"><div class="fill" style="width:{daily_pct}%;background:#4361ee"></div></div>
  </div>"""

    refresh_sec = 15 if email_running else 300
    refresh_ms = refresh_sec * 1000

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FTA Collection Dashboard</title>
{DASHBOARD_CSS}
<style>
@keyframes pulse {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.3; }}
}}
.live-indicator {{
  display: inline-block; width: 10px; height: 10px; background: #e74c3c;
  border-radius: 50%; margin-right: 6px; animation: pulse 1s infinite;
  vertical-align: middle;
}}
</style>
</head>
<body>
<h1>FTA Collection Dashboard</h1>
<p class="subtitle">Collection &amp; Email Monitor — Updated: {data['fetched_at']} | Auto-refresh: {refresh_sec}s</p>

<div class="nav">
  <a href="/dashboard/" class="active">Overview</a>
  <a href="/dashboard/health">Health History</a>
  <a href="/dashboard/segments">Country Rotation</a>
</div>

<div class="summary">
  <div class="card">
    <div class="label">Health Score</div>
    <div class="value {score_class}">{score_display}</div>
    <div class="sub">/ 100</div>
  </div>
  <div class="card">
    <div class="label">Today's New Leads</div>
    <div class="value">{today_leads}</div>
    <div class="sub">{data['total_leads']} total in CRM ({data['new_leads']} new)</div>
  </div>
  <div class="card">
    <div class="label">Next Collection</div>
    <div class="value" style="font-size:24px">{next_run_time or '--'}</div>
    <div class="sub">{next_run_str}</div>
  </div>
  <div class="card">
    <div class="label">Segmentation</div>
    <div class="value" style="font-size:24px">{seg_size}/day</div>
    <div class="sub">{seg_collected} countries tracked</div>
  </div>
  {email_card_html}
</div>

<div class="section-title">Health Trend (7 days)</div>
<div class="card" style="padding: 16px 20px 8px;">
  <div class="bar-chart">{trend_html or '<div class="empty-state">No health data yet — run a collection to start tracking</div>'}</div>
</div>

<div class="section-title">Recent Collection Runs</div>
<table>
  <thead><tr>
    <th>Started</th><th>Source</th><th>Status</th>
    <th>Found</th><th>New</th><th>Notes</th>
  </tr></thead>
  <tbody>
    {tasks_html or '<tr><td colspan="6" class="empty-state">No collection runs yet</td></tr>'}
  </tbody>
</table>

<div class="section-title">Country Collection Status</div>
<table>
  <thead><tr>
    <th>Country</th><th>Key</th><th>Last Collected</th>
    <th>Times</th><th>Last Leads</th><th>Errors</th>
  </tr></thead>
  <tbody>
    {seg_html or '<tr><td colspan="6" class="empty-state">No segment data yet</td></tr>'}
  </tbody>
</table>

<p class="refresh">Auto-refresh every {refresh_sec}s | <a href="/dashboard/api/data">JSON API</a></p>
<script>setTimeout(function(){{location.reload()}}, {refresh_ms});</script>
</body>
</html>"""

    return HTMLResponse(content=html)


# ═══════════════════════════════════════════════════════════════════════════
# Page: Health History
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/health", response_class=HTMLResponse)
async def dashboard_health():
    """Health history page — detailed metrics for past runs."""
    conn = _get_db()
    try:
        from collector.monitor import get_health_history, get_health_trend

        history = get_health_history(conn, days=30, limit=50)
        trend = get_health_trend(conn, days=14)

        # Build history table
        rows_html = ""
        for h in history:
            score = h.get("health_score", 0)
            badge = _health_badge(score)
            started = (h.get("started_at") or "")[:16]
            rows_html += (
                f"<tr>"
                f"<td>{started}</td>"
                f"<td>{badge}</td>"
                f"<td>{h.get('total_requests', 0)}</td>"
                f"<td>{h.get('avg_latency_ms', 0):.0f}ms</td>"
                f"<td>{h.get('error_count', 0)}</td>"
                f"<td>{h.get('rate_limit_count', 0)}</td>"
                f"<td>{h.get('countries_collected', 0)}/{h.get('countries_total', 0)}</td>"
                f"<td>{h.get('leads_found', 0)}</td>"
                f"<td>{h.get('leads_new', 0)}</td>"
                f"</tr>"
            )

        # Trend bars
        trend_html = ""
        if trend:
            max_score = max(t.get("avg_score", 0) for t in trend) or 100
            for t in trend:
                score = t.get("avg_score", 0)
                h_pct = max(4, int(score / max_score * 100))
                color = "#2ecc71" if score >= 80 else ("#f39c12" if score >= 50 else "#e74c3c")
                day = t.get("day", "")[-5:]
                trend_html += (
                    f'<div class="bar" style="height:{h_pct}%;background:{color}">'
                    f'<span class="tip">{day}: score={score:.0f}, {t.get("leads", 0)} leads</span>'
                    f'</div>'
                )
    finally:
        conn.close()

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Health History — FTA Dashboard</title>
{DASHBOARD_CSS}
</head>
<body>
<h1>Health History</h1>
<p class="subtitle">采集健康度历史记录</p>

<div class="nav">
  <a href="/dashboard/">Overview</a>
  <a href="/dashboard/health" class="active">Health History</a>
  <a href="/dashboard/segments">Country Rotation</a>
</div>

<div class="section-title">Daily Health Trend (14 days)</div>
<div class="card" style="padding: 16px 20px 8px;">
  <div class="bar-chart">{trend_html or '<div class="empty-state">No data yet</div>'}</div>
</div>

<div class="section-title">Run History (last 30 days)</div>
<table>
  <thead><tr>
    <th>Started</th><th>Health</th><th>Requests</th><th>Avg Latency</th>
    <th>Errors</th><th>Rate Limits</th><th>Countries</th>
    <th>Found</th><th>New</th>
  </tr></thead>
  <tbody>
    {rows_html or '<tr><td colspan="9" class="empty-state">No health records yet</td></tr>'}
  </tbody>
</table>

<p class="refresh">Auto-refresh every 5 min | <a href="/dashboard/api/data">JSON API</a></p>
<script>setTimeout(function(){{location.reload()}}, 300000);</script>
</body>
</html>"""
    return HTMLResponse(content=html)


# ═══════════════════════════════════════════════════════════════════════════
# Page: Country Rotation
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/segments", response_class=HTMLResponse)
async def dashboard_segments():
    """Country rotation page — shows which countries are due next."""
    conn = _get_db()
    try:
        from collector.segments import SegmentManager
        from collector.sources.xunkexing import DEFAULT_COUNTRY_KEYS

        mgr = SegmentManager(conn)
        all_status = mgr.get_rotation_status()
        next_batch = mgr.get_next_prediction(
            DEFAULT_COUNTRY_KEYS,
            config.COLLECTION_SEGMENT_SIZE,
        )

        # Country display names
        country_names = {
            "united_states": "USA", "germany": "Germany", "united_kingdom": "UK",
            "france": "France", "italy": "Italy", "spain": "Spain",
            "netherlands": "Netherlands", "poland": "Poland", "canada": "Canada",
            "australia": "Australia", "japan": "Japan", "south_korea": "South Korea",
            "brazil": "Brazil", "mexico": "Mexico", "turkey": "Turkey",
            "india": "India", "vietnam": "Vietnam", "thailand": "Thailand",
            "indonesia": "Indonesia", "malaysia": "Malaysia",
            "united_arab_emirates": "UAE", "saudi_arabia": "Saudi Arabia",
            "south_africa": "South Africa", "russia": "Russia",
        }

        now = datetime.now()
        rows_html = ""
        for seg in all_status:
            ck = seg["country_key"]
            name = country_names.get(ck, ck)
            last = seg.get("last_collected_at")
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    days_ago = (now - last_dt).days
                    last_display = f"{last[:10]} ({days_ago}d ago)"
                except (ValueError, TypeError):
                    last_display = str(last)[:10]
                    days_ago = 999
            else:
                last_display = "Never"
                days_ago = 999

            # Highlight next batch
            is_next = ck in next_batch
            row_style = 'style="background:#e8f0fe"' if is_next else ""

            # Urgency indicator
            if days_ago > 7 or last is None:
                urgency = '<span class="badge badge-bad">Due</span>'
            elif days_ago > 3:
                urgency = '<span class="badge badge-warn">Soon</span>'
            else:
                urgency = '<span class="badge badge-ok">OK</span>'

            next_mark = "NEXT" if is_next else ""
            rows_html += (
                f"<tr {row_style}>"
                f"<td><strong>{name}</strong> {next_mark}</td>"
                f"<td style='font-size:12px;color:#888'>{ck}</td>"
                f"<td>{last_display}</td>"
                f"<td>{urgency}</td>"
                f"<td>{seg.get('collection_count', 0)}</td>"
                f"<td>{seg.get('last_leads_found', 0)}</td>"
                f"<td>{seg.get('consecutive_errors', 0)}</td>"
                f"</tr>"
            )

        next_names = [country_names.get(c, c) for c in next_batch]
    finally:
        conn.close()

    seg_size = config.COLLECTION_SEGMENT_SIZE

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Country Rotation — FTA Dashboard</title>
{DASHBOARD_CSS}
</head>
<body>
<h1>Country Rotation Schedule</h1>
<p class="subtitle">国家分段采集轮换计划 — 每日采集 {seg_size} 个国家</p>

<div class="nav">
  <a href="/dashboard/">Overview</a>
  <a href="/dashboard/health">Health History</a>
  <a href="/dashboard/segments" class="active">Country Rotation</a>
</div>

<div class="summary">
  <div class="card">
    <div class="label">Next Batch</div>
    <div class="value" style="font-size:18px">{', '.join(next_names) if next_names else '--'}</div>
    <div class="sub">{len(next_batch)} countries selected for next run</div>
  </div>
  <div class="card">
    <div class="label">Countries Tracked</div>
    <div class="value">{len(all_status)}</div>
    <div class="sub">{seg_size} per day, ~{max(1, len(all_status) // seg_size)} day rotation</div>
  </div>
</div>

<div class="section-title">All Countries — Collection Status</div>
<table>
  <thead><tr>
    <th>Country</th><th>Key</th><th>Last Collected</th>
    <th>Urgency</th><th>Times</th><th>Last Leads</th><th>Errors</th>
  </tr></thead>
  <tbody>
    {rows_html or '<tr><td colspan="7" class="empty-state">No segment data yet</td></tr>'}
  </tbody>
</table>

<p style="color:#888;font-size:13px;">
  NEXT = countries that will be collected in the next scheduled run.<br>
  Priority: never collected > stale (>7 days) > least recently collected.<br>
  Error count resets to 0 after a successful collection.
</p>

<p class="refresh">Auto-refresh every 5 min | <a href="/dashboard/api/data">JSON API</a></p>
<script>setTimeout(function(){{location.reload()}}, 300000);</script>
</body>
</html>"""
    return HTMLResponse(content=html)
