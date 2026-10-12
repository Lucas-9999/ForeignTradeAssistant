"""
CRM Tagging — read-only tag / group listing for the CRM API (M1).

Schema V7 (M4 milestone) creates the tags / lead_tags / groups / group_members
tables. Before that migration runs, these endpoints return an explicit
`available: false` with an empty list so the UI can render a friendly
"标签功能尚未启用" state instead of a 500. This keeps the M1 read API stable
while M4 adds the write path underneath it — no contract change needed.

Blueprint 约定: customers.tags 已标记废弃, 不复用; 标签 v1 面向 leads.
"""

from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def tags_available(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "tags") and _table_exists(conn, "lead_tags")


def list_tags(conn: sqlite3.Connection) -> list[dict]:
    """List tags with per-tag lead counts. Empty until schema V7 (M4)."""
    if not tags_available(conn):
        return []
    rows = conn.execute(
        """SELECT t.id, t.name, t.category, t.color, t.created_at,
                  (SELECT COUNT(*) FROM lead_tags lt WHERE lt.tag_id = t.id) AS lead_count
           FROM tags t ORDER BY t.name"""
    ).fetchall()
    return [dict(r) for r in rows]


def groups_available(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "groups") and _table_exists(conn, "group_members")


def list_groups(conn: sqlite3.Connection) -> list[dict]:
    """List groups with member counts. Empty until schema V7 (M4)."""
    if not groups_available(conn):
        return []
    rows = conn.execute(
        """SELECT g.id, g.name, g.description, g.color, g.created_at,
                  (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS member_count
           FROM groups g ORDER BY g.name"""
    ).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# WRITE PATH (M4, schema V7)
# ═══════════════════════════════════════════════════════════════════════════
#
# 约定（Blueprint）:
#   - 所有写操作单事务提交, 幂等 (INSERT OR IGNORE / DELETE WHERE).
#   - bulk 上限 MAX_BULK=1000, 超出在 API 层拒绝 (422), 这里再防御一次.
#   - 用户错误用 ValueError 上抛 (重复名/空名), router 映射为 409/400.
#   - lead_id 先与真实 leads 求交集, 避免 FK 冲突 —— 不存在的 id 静默忽略.

MAX_BULK = 1000


def _existing_lead_ids(conn: sqlite3.Connection, lead_ids: list[int]) -> list[int]:
    """Dedupe + keep only ids that actually exist in leads (FK-safe)."""
    if not lead_ids:
        return []
    ids = sorted(set(int(i) for i in lead_ids))
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id FROM leads WHERE id IN ({placeholders})", ids
    ).fetchall()
    return [r["id"] for r in rows]


def _normalize_color(color: str | None) -> str:
    color = (color or "").strip()
    return color if color else "#64748b"


# ── Tags ───────────────────────────────────────────────────────────────────


def get_tag(conn: sqlite3.Connection, tag_id: int) -> dict | None:
    """Single tag with lead_count, or None if missing."""
    row = conn.execute(
        """SELECT t.id, t.name, t.category, t.color, t.created_at,
                  (SELECT COUNT(*) FROM lead_tags lt WHERE lt.tag_id = t.id) AS lead_count
           FROM tags t WHERE t.id = ?""",
        (tag_id,),
    ).fetchone()
    return dict(row) if row else None


def create_tag(
    conn: sqlite3.Connection,
    name: str,
    *,
    category: str = "custom",
    color: str | None = None,
) -> dict:
    """Create a tag. Raises ValueError on empty/duplicate name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("标签名不能为空")
    try:
        cur = conn.execute(
            "INSERT INTO tags (name, category, color) VALUES (?, ?, ?)",
            (name, category or "custom", _normalize_color(color)),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError(f"标签「{name}」已存在") from e
    return get_tag(conn, cur.lastrowid)


def update_tag(
    conn: sqlite3.Connection,
    tag_id: int,
    *,
    name: str | None = None,
    category: str | None = None,
    color: str | None = None,
) -> dict | None:
    """Update a tag's fields (only provided ones). Returns new row or None if
    the tag doesn't exist."""
    if not get_tag(conn, tag_id):
        return None
    sets, params = [], []
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("标签名不能为空")
        sets.append("name = ?")
        params.append(name)
    if category is not None:
        sets.append("category = ?")
        params.append(category)
    if color is not None:
        sets.append("color = ?")
        params.append(_normalize_color(color))
    if sets:
        try:
            conn.execute(
                f"UPDATE tags SET {', '.join(sets)} WHERE id = ?", [*params, tag_id]
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(f"标签「{name}」已存在") from e
    return get_tag(conn, tag_id)


def delete_tag(conn: sqlite3.Connection, tag_id: int) -> bool:
    """Delete a tag; lead_tags rows cascade. Returns False if tag missing."""
    cur = conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    conn.commit()
    return cur.rowcount > 0


def set_tag_leads(
    conn: sqlite3.Connection, tag_id: int, lead_ids: list[int]
) -> int:
    """Bulk assign a tag to leads. INSERT OR IGNORE (idempotent). Returns the
    number of NEW assignments. Raises ValueError if the tag doesn't exist."""
    if not get_tag(conn, tag_id):
        raise ValueError("标签不存在")
    ids = _existing_lead_ids(conn, lead_ids)[:MAX_BULK]
    if not ids:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO lead_tags (lead_id, tag_id) VALUES (?, ?)",
        [(lid, tag_id) for lid in ids],
    )
    conn.commit()
    return cur.rowcount


def remove_tag_leads(
    conn: sqlite3.Connection, tag_id: int, lead_ids: list[int]
) -> int:
    """Remove a tag from leads. Idempotent. Returns number of rows removed."""
    if not get_tag(conn, tag_id):
        raise ValueError("标签不存在")
    ids = _existing_lead_ids(conn, lead_ids)[:MAX_BULK]
    if not ids:
        return 0
    cur = conn.executemany(
        "DELETE FROM lead_tags WHERE lead_id = ? AND tag_id = ?",
        [(lid, tag_id) for lid in ids],
    )
    conn.commit()
    return cur.rowcount


# ── Groups ─────────────────────────────────────────────────────────────────


def get_group(conn: sqlite3.Connection, group_id: int) -> dict | None:
    """Single group with member_count, or None if missing."""
    row = conn.execute(
        """SELECT g.id, g.name, g.description, g.color, g.created_at,
                  (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.id) AS member_count
           FROM groups g WHERE g.id = ?""",
        (group_id,),
    ).fetchone()
    return dict(row) if row else None


def create_group(
    conn: sqlite3.Connection,
    name: str,
    *,
    description: str | None = None,
    color: str | None = None,
) -> dict:
    """Create a group. Raises ValueError on empty/duplicate name."""
    name = (name or "").strip()
    if not name:
        raise ValueError("分组名不能为空")
    try:
        cur = conn.execute(
            "INSERT INTO groups (name, description, color) VALUES (?, ?, ?)",
            (name, description, _normalize_color(color)),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError(f"分组「{name}」已存在") from e
    return get_group(conn, cur.lastrowid)


def update_group(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    color: str | None = None,
) -> dict | None:
    """Update a group's fields. Returns new row or None if group missing."""
    if not get_group(conn, group_id):
        return None
    sets, params = [], []
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("分组名不能为空")
        sets.append("name = ?")
        params.append(name)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if color is not None:
        sets.append("color = ?")
        params.append(_normalize_color(color))
    if sets:
        try:
            conn.execute(
                f"UPDATE groups SET {', '.join(sets)} WHERE id = ?", [*params, group_id]
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(f"分组「{name}」已存在") from e
    return get_group(conn, group_id)


def delete_group(conn: sqlite3.Connection, group_id: int) -> bool:
    """Delete a group; group_members rows cascade. Returns False if missing."""
    cur = conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()
    return cur.rowcount > 0


def set_group_members(
    conn: sqlite3.Connection, group_id: int, lead_ids: list[int]
) -> int:
    """Bulk add leads to a group. INSERT OR IGNORE (idempotent)."""
    if not get_group(conn, group_id):
        raise ValueError("分组不存在")
    ids = _existing_lead_ids(conn, lead_ids)[:MAX_BULK]
    if not ids:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO group_members (lead_id, group_id) VALUES (?, ?)",
        [(lid, group_id) for lid in ids],
    )
    conn.commit()
    return cur.rowcount


def remove_group_members(
    conn: sqlite3.Connection, group_id: int, lead_ids: list[int]
) -> int:
    """Remove leads from a group. Idempotent."""
    if not get_group(conn, group_id):
        raise ValueError("分组不存在")
    ids = _existing_lead_ids(conn, lead_ids)[:MAX_BULK]
    if not ids:
        return 0
    cur = conn.executemany(
        "DELETE FROM group_members WHERE lead_id = ? AND group_id = ?",
        [(lid, group_id) for lid in ids],
    )
    conn.commit()
    return cur.rowcount
