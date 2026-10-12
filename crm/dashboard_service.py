"""
CRM Dashboard Service — KPI / 30-day series / distribution queries (read-only).

Queries the live tables directly (70k-scale SQLite aggregates are <100ms).
The response contract is stable — a future daily_metrics snapshot table can
replace the source without changing what the frontend receives.

Timezone note: all "today" windows use SQLite date('now') = UTC day, matching
the existing quota tables (sending_quotas / mailbox_quotas), so dashboard
numbers agree with the anti-spam guard exactly. CST is UTC+8.
"""

from __future__ import annotations

import datetime
import math
import sqlite3

from emailer.inbox import classify_persisted_inbound, _strip_reply_prefixes

# ── Hard-bounce predicate — mirrors emailer/sender.py:_is_hard_bounce ─────
# sender records hard bounces with a "HARD_BOUNCE:" prefix; fall back to the
# SMTP 5xx / 5.x.x patterns for legacy rows recorded before that prefix.
_BOUNCE_SQL = (
    "(error_message LIKE 'HARD_BOUNCE%' "
    "OR error_message LIKE '%bounced%' OR error_message LIKE '%refused%' "
    "OR error_message LIKE '%user unknown%' OR error_message LIKE '%no such user%' "
    "OR error_message LIKE '%not delivered%' "
    "OR error_message GLOB '*5[0-9][0-9]*' "
    "OR error_message GLOB '*5.[0-9].[0-9]*')"
)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


def _classified_inbound_rows(
    conn: sqlite3.Connection,
    *,
    today_only: bool = False,
) -> list[dict]:
    where = "AND date(c.sent_at)=date('now')" if today_only else ""
    rows = conn.execute(
        f"""SELECT c.*, l.company_name, l.email
            FROM communications c
            LEFT JOIN leads l ON l.id=c.lead_id
            WHERE c.direction='inbound' {where}
            ORDER BY c.sent_at DESC, c.id DESC"""
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        # Bounces persisted by the IMAP monitor carry status='bounced' — trust
        # that flag even when the stored subject/body no longer matches the
        # keyword lists (some DSNs are classified live by From/Return-Path only,
        # which a persisted row cannot re-derive).
        if item.get("status") == "bounced":
            item["classification"] = "bounce"
        else:
            item["classification"] = classify_persisted_inbound(
                item.get("subject"), item.get("body")
            ).value
        result.append(item)
    return result


def _genuine_reply_rows(
    conn: sqlite3.Connection,
    *,
    today_only: bool = False,
) -> list[dict]:
    """Genuine FTA replies, one latest row per normalized conversation.

    The database also contains imported personal-mailbox history from before
    automated FTA outreach began. The first linked outbound communication is
    the stable boundary for the FTA campaign. Multiple messages in one thread
    count as one reply conversation.
    """
    campaign_start = _scalar(
        conn,
        """SELECT MIN(sent_at) FROM communications
           WHERE direction='outbound' AND lead_id IS NOT NULL""",
    )
    seen_threads: set[str] = set()
    replies: list[dict] = []
    for row in _classified_inbound_rows(conn, today_only=today_only):
        if row["classification"] != "reply":
            continue
        if campaign_start and str(row.get("sent_at") or "") < str(campaign_start):
            continue
        thread_key = _strip_reply_prefixes(row.get("subject") or "").strip().lower()
        thread_key = thread_key or f"message:{row['id']}"
        if thread_key in seen_threads:
            continue
        seen_threads.add(thread_key)
        replies.append(row)
    return replies


def get_kpis(conn: sqlite3.Connection) -> dict:
    """Today + cumulative KPIs for the dashboard header cards."""
    today_sent = _scalar(
        conn,
        "SELECT COALESCE((SELECT email_count FROM sending_quotas WHERE date=date('now')), 0) "
        "     + COALESCE((SELECT SUM(email_count) FROM mailbox_quotas WHERE date=date('now')), 0)",
    )
    inbound_today = _classified_inbound_rows(conn, today_only=True)
    today_replies = len(_genuine_reply_rows(conn, today_only=True))
    today_bounces = sum(1 for row in inbound_today if row["classification"] == "bounce")
    today_bounces += _scalar(
        conn,
        f"SELECT COUNT(*) FROM communications "
        f"WHERE status='failed' AND date(sent_at)=date('now') AND {_BOUNCE_SQL}",
    )
    totals = conn.execute(
        """SELECT (SELECT COUNT(*) FROM leads) AS total_leads,
                  (SELECT COUNT(*) FROM customers) AS total_customers,
                  (SELECT COUNT(DISTINCT lead_id) FROM communications
                    WHERE channel='email' AND direction='outbound'
                      AND lead_id IS NOT NULL) AS contacted_leads,
                  0 AS replied_leads"""
    ).fetchone()
    genuine_replies = len(_genuine_reply_rows(conn))

    return {
        "today_new_leads": _scalar(
            conn, "SELECT COUNT(*) FROM leads WHERE date(discovered_at)=date('now')"
        ),
        "today_emails_sent": today_sent,
        "today_replies": today_replies,
        "today_bounces": today_bounces,
        "total_leads": totals["total_leads"],
        "total_customers": totals["total_customers"],
        "contacted_leads": totals["contacted_leads"],
        # Kept under the stable API key; value now means genuine reply emails
        # rather than every legacy inbound/DSN row.
        "replied_leads": genuine_replies,
    }


def _series_30d(conn: sqlite3.Connection) -> dict:
    """Four zero-filled 30-day series (UTC days). Keys: leads_created / emails_sent / replies / bounces."""
    start = (datetime.datetime.now(datetime.timezone.utc).date()
             - datetime.timedelta(days=29)).isoformat()
    buckets: list[str] = []
    day = datetime.datetime.now(datetime.timezone.utc).date()
    for _ in range(30):
        buckets.append(day.isoformat())
        day -= datetime.timedelta(days=1)
    buckets.reverse()

    def _fill(sql: str, params: tuple = ()) -> list[dict]:
        agg: dict[str, int] = {}
        for r in conn.execute(sql, (start, *params)):
            agg[r["day"]] = r["n"]
        return [{"day": b, "count": agg.get(b, 0)} for b in buckets]

    leads_created = _fill(
        """SELECT date(discovered_at) AS day, COUNT(*) AS n FROM leads
           WHERE discovered_at >= ? GROUP BY date(discovered_at)"""
    )
    emails_sent = _fill(
        """SELECT day, SUM(n) AS n FROM (
               SELECT date AS day, email_count AS n FROM sending_quotas
                WHERE date >= ?
             UNION ALL
               SELECT date, email_count FROM mailbox_quotas
                WHERE date >= ?)
           GROUP BY day ORDER BY day""",
        (start,),  # second '?' re-uses the same start
    )
    inbound_by_day: dict[str, dict[str, int]] = {}
    genuine_reply_ids = {row["id"] for row in _genuine_reply_rows(conn)}
    for row in _classified_inbound_rows(conn):
        day_key = str(row.get("sent_at") or "")[:10]
        if day_key < start:
            continue
        counts = inbound_by_day.setdefault(day_key, {"reply": 0, "bounce": 0})
        classification = row["classification"]
        if classification == "reply" and row["id"] not in genuine_reply_ids:
            continue
        if classification in counts:
            counts[classification] += 1
    replies = [
        {"day": bucket, "count": inbound_by_day.get(bucket, {}).get("reply", 0)}
        for bucket in buckets
    ]
    bounces = _fill(
        f"""SELECT date(sent_at) AS day, COUNT(*) AS n FROM communications
            WHERE status='failed' AND sent_at >= ? AND {_BOUNCE_SQL}
            GROUP BY date(sent_at)"""
    )
    for point in bounces:
        point["count"] += inbound_by_day.get(point["day"], {}).get("bounce", 0)
    return {
        "leads_created": leads_created,
        "emails_sent": emails_sent,
        "replies": replies,
        "bounces": bounces,
    }


def get_distributions(conn: sqlite3.Connection) -> dict:
    """Category counts for charts: source / country(top15) / status / mailbox(30d)."""
    by_source = [
        {"name": r["source"] or "unknown", "count": r["n"]}
        for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM leads GROUP BY source ORDER BY n DESC"
        ).fetchall()
    ]
    by_country = [
        {"name": r["country"], "count": r["n"]}
        for r in conn.execute(
            """SELECT country, COUNT(*) AS n FROM leads
               WHERE country IS NOT NULL AND TRIM(country) <> ''
               GROUP BY country ORDER BY n DESC LIMIT 15"""
        ).fetchall()
    ]
    by_status = [
        {"name": r["status"] or "unknown", "count": r["n"]}
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM leads GROUP BY status ORDER BY n DESC"
        ).fetchall()
    ]
    by_mailbox = [
        {"name": r["mailbox"], "count": r["n"]}
        for r in conn.execute(
            """SELECT mailbox, SUM(email_count) AS n FROM mailbox_quotas
               WHERE date >= date('now', '-30 days')
               GROUP BY mailbox ORDER BY n DESC"""
        ).fetchall()
    ]
    # The default/NetEase counter lives in sending_quotas, not mailbox_quotas.
    ne_30d = _scalar(
        conn,
        "SELECT COALESCE(SUM(email_count),0) FROM sending_quotas "
        "WHERE date >= date('now', '-30 days')",
    )
    if ne_30d:
        by_mailbox.append({"name": "default", "count": ne_30d})
    return {
        "by_source": by_source,
        "by_country": by_country,
        "by_status": by_status,
        "by_mailbox": by_mailbox,
    }


def get_dashboard_overview(conn: sqlite3.Connection) -> dict:
    """Full /api/v1/dashboard/overview payload."""
    return {
        "kpis": get_kpis(conn),
        "series_30d": _series_30d(conn),
        "distributions": get_distributions(conn),
    }


_DETAIL_TITLES = {
    "today_new_leads": "今日新增线索",
    "today_emails_sent": "今日发送邮件",
    "today_replies": "今日真实回复",
    "today_bounces": "今日退信",
    "total_leads": "全部线索",
    "total_customers": "全部客户",
    "contacted_leads": "已联系线索",
    "replied_leads": "真实回复邮件",
}


def _detail_item(row: dict, kind: str, classification: str = "") -> dict:
    return {
        "id": row.get("id"),
        "kind": kind,
        "lead_id": row.get("lead_id") or (row.get("id") if kind == "lead" else None),
        "company_name": row.get("company_name"),
        "email": row.get("email"),
        "country": row.get("country"),
        "source": row.get("source"),
        "status": row.get("status"),
        "subject": row.get("subject"),
        "classification": classification,
        "occurred_at": row.get("sent_at") or row.get("discovered_at") or row.get("created_at"),
    }


def get_dashboard_details(
    conn: sqlite3.Connection,
    metric: str,
    *,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Return paginated rows behind a dashboard KPI card."""
    if metric not in _DETAIL_TITLES:
        raise KeyError(metric)

    items: list[dict]
    if metric in {"today_replies", "today_bounces", "replied_leads"}:
        target = "bounce" if metric == "today_bounces" else "reply"
        rows = (
            _classified_inbound_rows(conn, today_only=True)
            if target == "bounce"
            else _genuine_reply_rows(conn, today_only=metric.startswith("today_"))
        )
        items = [
            _detail_item(row, "communication", row["classification"])
            for row in rows if row["classification"] == target
        ]
        if metric == "today_bounces":
            failed = conn.execute(
                f"""SELECT c.*, l.company_name, l.email
                    FROM communications c LEFT JOIN leads l ON l.id=c.lead_id
                    WHERE c.status='failed' AND date(c.sent_at)=date('now')
                      AND {_BOUNCE_SQL}
                    ORDER BY c.sent_at DESC, c.id DESC"""
            ).fetchall()
            items.extend(_detail_item(dict(row), "communication", "bounce") for row in failed)
            items.sort(key=lambda row: row.get("occurred_at") or "", reverse=True)
    elif metric == "today_emails_sent":
        rows = conn.execute(
            """SELECT c.*, l.company_name, l.email FROM communications c
               LEFT JOIN leads l ON l.id=c.lead_id
               WHERE c.direction='outbound' AND c.status IN ('sent','delivered','replied')
                 AND date(c.sent_at)=date('now')
               ORDER BY c.sent_at DESC, c.id DESC"""
        ).fetchall()
        items = [_detail_item(dict(row), "communication") for row in rows]
    elif metric == "total_customers":
        rows = conn.execute("SELECT * FROM customers ORDER BY created_at DESC, id DESC").fetchall()
        items = [_detail_item(dict(row), "customer") for row in rows]
    else:
        conditions = []
        if metric == "today_new_leads":
            conditions.append("date(l.discovered_at)=date('now')")
        elif metric == "contacted_leads":
            conditions.append(
                "EXISTS (SELECT 1 FROM communications c WHERE c.lead_id=l.id "
                "AND c.channel='email' AND c.direction='outbound')"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = conn.execute(
            f"SELECT l.* FROM leads l {where} ORDER BY l.discovered_at DESC, l.id DESC"
        ).fetchall()
        items = [_detail_item(dict(row), "lead") for row in rows]

    total = len(items)
    kpi_total = int(get_kpis(conn)[metric])
    start_index = (page - 1) * page_size
    return {
        "metric": metric,
        "title": _DETAIL_TITLES[metric],
        # Quota tables are authoritative for today's send KPI, while old send
        # paths did not always create a communications row. Expose both values
        # so the UI can explain that gap instead of silently hiding it.
        "kpi_total": kpi_total,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(total / page_size)),
        "items": items[start_index:start_index + page_size],
    }
