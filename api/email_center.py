"""M6 Email Center API — safe preview, confirmation and background send."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from crm.db import get_db
from emailer import email_center

router = APIRouter(prefix="/api/v1/email-center", tags=["email-center"])


class EmailCenterPreviewRequest(BaseModel):
    mailbox: str = Field(default="default", min_length=1, max_length=50)
    limit: int = Field(default=10, ge=1, le=email_center.MAX_BATCH_SIZE)
    country_filter: Optional[str] = Field(default=None, max_length=100)
    country_group: Optional[str] = Field(default=None, max_length=50)
    industry_group: Optional[str] = Field(default=None, max_length=50)
    include_low_relevance: bool = False
    category: Optional[str] = Field(default=None, max_length=100)
    template_id: Optional[str] = Field(default=None, max_length=100)
    source: Optional[str] = Field(default=None, max_length=100)
    lead_ids: list[int] = Field(default_factory=list, max_length=200)


class EmailCenterSendRequest(BaseModel):
    confirmation_id: str = Field(min_length=10, max_length=200)
    confirmation_text: str = Field(min_length=1, max_length=100)


def _raise(exc: email_center.EmailCenterError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/options")
async def options():
    conn = get_db()
    try:
        return email_center.get_options(conn)
    finally:
        conn.close()


@router.post("/preview")
async def preview(data: EmailCenterPreviewRequest):
    conn = get_db()
    try:
        try:
            return email_center.create_preview(conn, data.model_dump())
        except email_center.EmailCenterError as exc:
            _raise(exc)
    finally:
        conn.close()


@router.post("/send", status_code=202)
async def send(
    data: EmailCenterSendRequest,
    x_fta_send_token: Optional[str] = Header(default=None, alias="X-FTA-Send-Token"),
):
    conn = get_db()
    try:
        return email_center.start_send(
            conn,
            data.confirmation_id,
            data.confirmation_text,
            x_fta_send_token,
        )
    except email_center.EmailCenterError as exc:
        _raise(exc)
    finally:
        conn.close()


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = email_center.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="发送任务不存在")
    return job


@router.get("/audit")
async def audit(limit: int = Query(default=20, ge=1, le=100)):
    return {"items": email_center.recent_audit(limit)}
