"""Tags router — /api/v1/tags (M1 read + M4 write).

- GET    ""                — list tags with lead counts ({available, tags})
- POST   ""                — create tag (409 on duplicate name)
- PUT    /{tag_id}         — rename / recolor / recategorize (404 / 409)
- DELETE /{tag_id}         — delete; lead_tags rows cascade
- POST   /{tag_id}/leads   — bulk assign to leads (INSERT OR IGNORE, ≤1000)
- DELETE /{tag_id}/leads   — bulk unassign

Validation rule: lead_ids ∩ real leads (nonexistent ids silently ignored),
duplicate names → 409, bulk >1000 → 422 (FastAPI Field bound).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from crm.db import get_db
from crm import tagging

router = APIRouter(prefix="/api/v1/tags", tags=["tags"])


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    category: str = "custom"
    color: str = "#64748b"


class TagUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    category: Optional[str] = None
    color: Optional[str] = None


class TagLeadsRequest(BaseModel):
    lead_ids: list[int] = Field(min_length=1, max_length=tagging.MAX_BULK)


def _conflict_or_400(e: ValueError) -> HTTPException:
    """Map ValueError to an HTTP error: duplicate name → 409, other → 400."""
    msg = str(e)
    if "已存在" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=400, detail=msg)


@router.get("")
async def get_tags():
    """List tags with lead counts."""
    conn = get_db()
    try:
        return {"available": tagging.tags_available(conn), "tags": tagging.list_tags(conn)}
    finally:
        conn.close()


@router.post("")
async def create_tag(data: TagCreate):
    """Create a new tag. 409 if the name already exists."""
    conn = get_db()
    try:
        try:
            return tagging.create_tag(
                conn, data.name, category=data.category, color=data.color
            )
        except ValueError as e:
            raise _conflict_or_400(e) from e
    finally:
        conn.close()


@router.put("/{tag_id}")
async def update_tag(tag_id: int, data: TagUpdate):
    """Update a tag (only provided fields). 404 missing / 409 duplicate name."""
    conn = get_db()
    try:
        try:
            tag = tagging.update_tag(
                conn, tag_id,
                name=data.name, category=data.category, color=data.color,
            )
        except ValueError as e:
            raise _conflict_or_400(e) from e
        if not tag:
            raise HTTPException(status_code=404, detail=f"标签 #{tag_id} 不存在")
        return tag
    finally:
        conn.close()


@router.delete("/{tag_id}")
async def delete_tag(tag_id: int):
    """Delete a tag (lead_tags cascade). 404 if missing."""
    conn = get_db()
    try:
        if not tagging.delete_tag(conn, tag_id):
            raise HTTPException(status_code=404, detail=f"标签 #{tag_id} 不存在")
        return {"deleted": True, "tag_id": tag_id}
    finally:
        conn.close()


@router.post("/{tag_id}/leads")
async def add_tag_leads(tag_id: int, data: TagLeadsRequest):
    """Bulk assign a tag to leads. Idempotent — returns NEW assignments."""
    conn = get_db()
    try:
        if not tagging.get_tag(conn, tag_id):
            raise HTTPException(status_code=404, detail=f"标签 #{tag_id} 不存在")
        assigned = tagging.set_tag_leads(conn, tag_id, data.lead_ids)
        return {"tag_id": tag_id, "assigned": assigned}
    finally:
        conn.close()


@router.delete("/{tag_id}/leads")
async def remove_tag_leads(tag_id: int, data: TagLeadsRequest):
    """Bulk unassign a tag from leads. Idempotent."""
    conn = get_db()
    try:
        if not tagging.get_tag(conn, tag_id):
            raise HTTPException(status_code=404, detail=f"标签 #{tag_id} 不存在")
        removed = tagging.remove_tag_leads(conn, tag_id, data.lead_ids)
        return {"tag_id": tag_id, "removed": removed}
    finally:
        conn.close()
