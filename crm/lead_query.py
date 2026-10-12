"""
CRM Lead Query — server-side pagination / filter builder for the CRM API.

Read-only complement to crm.manager.LeadManager.list_all with:
  - page/page_size pagination + total count (70k-scale SQLite aggregate, <100ms)
  - contact flags DERIVED from communications (has_sent / has_replied), never
    from denormalized fields — inbound rows are the authoritative reply record
  - country / tag / sort filters; sort goes through a column whitelist so user
    input is never interpolated into SQL
  - tags[] for a page of ids resolved in ONE IN query (no N+1)

Blue 约定: 已发/已回复标记一律从 communications 推导 (has_contacted), 不冗余.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional

logger = logging.getLogger("fta.crm.lead_query")

# ── LIKE escaping (user input must not act as wildcards) ───────────────────
_LIKE_ESCAPE_RE = re.compile(r"([%_])")


def escape_like(s: str) -> str:
    """Escape LIKE wildcards so user search text is matched literally."""
    return _LIKE_ESCAPE_RE.sub(r"\\\1", s)


# ── Sort whitelist: column_name -> allowed sort keys (prefixed '-' = DESC) ─
_SORT_COLUMNS = {
    "id": "l.id",
    "company_name": "l.company_name",
    "email": "l.email",
    "status": "l.status",
    "source": "l.source",
    "country": "l.country",
    "industry": "l.industry",
    "discovered_at": "l.discovered_at",
    "created_at": "l.created_at",
    "updated_at": "l.updated_at",
    "first_contacted_at": "l.first_contacted_at",
    "last_contacted_at": "l.last_contacted_at",
}


def _order_clause(sort: Optional[str]) -> tuple[str, list]:
    """Map a sort key to a safe ORDER BY fragment (never user SQL)."""
    if not sort:
        return "l.id DESC", []
    desc = sort.startswith("-")
    key = sort[1:] if desc else sort
    col = _SORT_COLUMNS.get(key)
    if col is None:
        # Unknown sort key — fall back to id DESC rather than erroring out.
        logger.warning("Unknown sort key %r, falling back to id DESC", sort)
        return "l.id DESC", []
    return f"{col} {'DESC' if desc else 'ASC'}", []


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _load_tags_for(conn: sqlite3.Connection, lead_ids: list[int]) -> dict[int, list[dict]]:
    """Resolve tags for a page of lead ids in one query.

    Schema V7 (M4) adds tags/lead_tags. Before then the table is absent and
    every lead simply has no tags. Returns {lead_id: [TagInfo.to_dict(), ...]}.
    """
    if not lead_ids or not _table_exists(conn, "lead_tags"):
        return {}
    placeholders = ",".join("?" * len(lead_ids))
    rows = conn.execute(
        f"""SELECT lt.lead_id, t.id, t.name, t.category, t.color
            FROM lead_tags lt JOIN tags t ON t.id = lt.tag_id
            WHERE lt.lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall()
    out: dict[int, list[dict]] = {lid: [] for lid in lead_ids}
    for r in rows:
        out.setdefault(r["lead_id"], []).append(
            {"id": r["id"], "name": r["name"], "category": r["category"],
             "color": r["color"]}
        )
    return out


def _load_groups_for(conn: sqlite3.Connection, lead_ids: list[int]) -> dict[int, list[dict]]:
    """Resolve group membership for a page of lead ids in one query (M4)."""
    if not lead_ids or not _table_exists(conn, "group_members"):
        return {}
    placeholders = ",".join("?" * len(lead_ids))
    rows = conn.execute(
        f"""SELECT gm.lead_id, g.id, g.name, g.color
            FROM group_members gm JOIN groups g ON g.id = gm.group_id
            WHERE gm.lead_id IN ({placeholders})""",
        lead_ids,
    ).fetchall()
    out: dict[int, list[dict]] = {lid: [] for lid in lead_ids}
    for r in rows:
        out.setdefault(r["lead_id"], []).append(
            {"id": r["id"], "name": r["name"], "color": r["color"]}
        )
    return out


def query_leads(
    conn: sqlite3.Connection,
    *,
    page: int = 1,
    page_size: int = 50,
    search: Optional[str] = None,
    country: Optional[str] = None,
    source: Optional[str] = None,
    status: Optional[str] = None,
    tags: Optional[list[str]] = None,
    has_sent: Optional[bool] = None,
    has_replied: Optional[bool] = None,
    sort: Optional[str] = None,
) -> tuple[list[dict], int]:
    """Query leads with pagination + filters. Returns (items, total).

    `items` are dicts with all lead columns plus derived flags:
      has_sent / has_replied / sent_count / reply_count / last_activity_at / tags[]
    """
    conditions: list[str] = []  # no dedup filter — keeps legacy lead counts
    params: list = []

    if search:
        like = f"%{escape_like(search)}%"
        conditions.append(
            "(l.company_name LIKE ? ESCAPE '\\' OR l.contact_name LIKE ? ESCAPE '\\' "
            "OR l.email LIKE ? ESCAPE '\\')"
        )
        params.extend([like, like, like])
    if country:
        conditions.append("l.country = ?")
        params.append(country)
    if source:
        conditions.append("l.source = ?")
        params.append(source)
    if status:
        conditions.append("l.status = ?")
        params.append(status)

    if has_sent is True:
        conditions.append(
            "EXISTS (SELECT 1 FROM communications c WHERE c.lead_id = l.id "
            "AND c.channel='email' AND c.direction='outbound')"
        )
    elif has_sent is False:
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM communications c WHERE c.lead_id = l.id "
            "AND c.channel='email' AND c.direction='outbound')"
        )
    if has_replied is True:
        conditions.append(
            "EXISTS (SELECT 1 FROM communications c WHERE c.lead_id = l.id "
            "AND c.direction='inbound')"
        )
    elif has_replied is False:
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM communications c WHERE c.lead_id = l.id "
            "AND c.direction='inbound')"
        )

    # Tag filter — only applies once schema V7 (M4) creates tags/lead_tags.
    # Multi-tag is AND semantics (documented API contract): each requested tag
    # gets its own EXISTS so a lead must carry ALL of them. dict.fromkeys
    # dedups request tags in order (duplicates would otherwise skew the AND).
    if tags and _table_exists(conn, "lead_tags"):
        for t in dict.fromkeys(tags):
            conditions.append(
                "EXISTS (SELECT 1 FROM lead_tags lt JOIN tags tg ON tg.id=lt.tag_id "
                "WHERE lt.lead_id = l.id AND tg.name = ?)"
            )
            params.append(t)

    where = " AND ".join(conditions) if conditions else "1=1"
    order, _ = _order_clause(sort)

    total = conn.execute(
        f"SELECT COUNT(*) FROM leads l WHERE {where}", params
    ).fetchone()[0]

    page_size = max(1, min(int(page_size), 200))
    page = max(1, int(page))
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"""SELECT l.*,
                   (SELECT COUNT(*) FROM communications c
                     WHERE c.lead_id = l.id AND c.channel='email'
                       AND c.direction='outbound') AS sent_count,
                   (SELECT COUNT(*) FROM communications c
                     WHERE c.lead_id = l.id AND c.direction='inbound') AS reply_count,
                   (SELECT MAX(c.sent_at) FROM communications c
                     WHERE c.lead_id = l.id) AS last_activity_at
              FROM leads l
             WHERE {where}
             ORDER BY {order}
             LIMIT ? OFFSET ?""",
        [*params, page_size, offset],
    ).fetchall()

    tags_map = _load_tags_for(conn, [r["id"] for r in rows])
    groups_map = _load_groups_for(conn, [r["id"] for r in rows])
    items = []
    for r in rows:
        d = dict(r)
        d["has_sent"] = d["sent_count"] > 0
        d["has_replied"] = d["reply_count"] > 0
        d["tags"] = tags_map.get(d["id"], [])
        d["groups"] = groups_map.get(d["id"], [])
        items.append(d)

    return items, total
