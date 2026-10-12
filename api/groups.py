"""Groups router — /api/v1/groups (M1 read + M4 write).

Same contract as tags: GET "" / POST "" / PUT /{id} / DELETE /{id} /
POST|DELETE /{id}/leads for membership. Duplicate names → 409, bulk ≤1000.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from crm.db import get_db
from crm import tagging

router = APIRouter(prefix="/api/v1/groups", tags=["groups"])


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: Optional[str] = Field(default=None, max_length=200)
    color: str = "#64748b"


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=50)
    description: Optional[str] = Field(default=None, max_length=200)
    color: Optional[str] = None


class GroupLeadsRequest(BaseModel):
    lead_ids: list[int] = Field(min_length=1, max_length=tagging.MAX_BULK)


def _conflict_or_400(e: ValueError) -> HTTPException:
    msg = str(e)
    if "已存在" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=400, detail=msg)


@router.get("")
async def get_groups():
    """List groups with member counts."""
    conn = get_db()
    try:
        return {"available": tagging.groups_available(conn), "groups": tagging.list_groups(conn)}
    finally:
        conn.close()


@router.post("")
async def create_group(data: GroupCreate):
    """Create a new group. 409 if the name already exists."""
    conn = get_db()
    try:
        try:
            return tagging.create_group(
                conn, data.name, description=data.description, color=data.color
            )
        except ValueError as e:
            raise _conflict_or_400(e) from e
    finally:
        conn.close()


@router.put("/{group_id}")
async def update_group(group_id: int, data: GroupUpdate):
    """Update a group (only provided fields). 404 missing / 409 duplicate name."""
    conn = get_db()
    try:
        try:
            group = tagging.update_group(
                conn, group_id,
                name=data.name, description=data.description, color=data.color,
            )
        except ValueError as e:
            raise _conflict_or_400(e) from e
        if not group:
            raise HTTPException(status_code=404, detail=f"分组 #{group_id} 不存在")
        return group
    finally:
        conn.close()


@router.delete("/{group_id}")
async def delete_group(group_id: int):
    """Delete a group (group_members cascade). 404 if missing."""
    conn = get_db()
    try:
        if not tagging.delete_group(conn, group_id):
            raise HTTPException(status_code=404, detail=f"分组 #{group_id} 不存在")
        return {"deleted": True, "group_id": group_id}
    finally:
        conn.close()


@router.post("/{group_id}/leads")
async def add_group_members(group_id: int, data: GroupLeadsRequest):
    """Bulk add leads to a group. Idempotent — returns NEW memberships."""
    conn = get_db()
    try:
        if not tagging.get_group(conn, group_id):
            raise HTTPException(status_code=404, detail=f"分组 #{group_id} 不存在")
        assigned = tagging.set_group_members(conn, group_id, data.lead_ids)
        return {"group_id": group_id, "assigned": assigned}
    finally:
        conn.close()


@router.delete("/{group_id}/leads")
async def remove_group_members(group_id: int, data: GroupLeadsRequest):
    """Bulk remove leads from a group. Idempotent."""
    conn = get_db()
    try:
        if not tagging.get_group(conn, group_id):
            raise HTTPException(status_code=404, detail=f"分组 #{group_id} 不存在")
        removed = tagging.remove_group_members(conn, group_id, data.lead_ids)
        return {"group_id": group_id, "removed": removed}
    finally:
        conn.close()
