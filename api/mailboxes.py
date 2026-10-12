"""Mailboxes router — /api/v1/mailboxes (M1 read + M5 write).

- GET    ""            — list mailboxes + today's quota ({mailboxes})
- POST   ""            — add an arbitrary SMTP account (409 duplicate / reserved)
- PUT    /{name}       — update a mailbox (only provided fields; 404 missing)
- PATCH  /{name}       — alias of PUT
- DELETE /{name}       — delete (409 reserved / 409 quota>0 today)
- POST   /{name}/test  — connect+login test via stdlib smtplib (sends NOTHING)

Password is write-only: it appears in request bodies but NEVER in responses —
the API returns only has_smtp_password. Without FTA_MAILBOX_ENC_KEY, password
fields return 503 with a Chinese message (sending still falls back to env).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from crm.db import get_db
from crm import mailbox_manager

router = APIRouter(prefix="/api/v1/mailboxes", tags=["mailboxes"])


class MailboxCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    display_name: Optional[str] = Field(default=None, max_length=100)
    smtp_host: str = Field(min_length=1, max_length=200)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    use_tls: bool = True
    username: str = Field(min_length=1, max_length=200)
    password: Optional[str] = Field(default=None, max_length=300)
    from_email: Optional[str] = Field(default=None, max_length=200)
    from_name: Optional[str] = Field(default=None, max_length=100)
    daily_limit: Optional[int] = Field(default=None, ge=1, le=100000)


class MailboxUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=100)
    smtp_host: Optional[str] = Field(default=None, max_length=200)
    smtp_port: Optional[int] = Field(default=None, ge=1, le=65535)
    use_tls: Optional[bool] = None
    username: Optional[str] = Field(default=None, max_length=200)
    password: Optional[str] = Field(default=None, max_length=300)
    from_email: Optional[str] = Field(default=None, max_length=200)
    from_name: Optional[str] = Field(default=None, max_length=100)
    daily_limit: Optional[int] = Field(default=None, ge=1, le=100000)
    is_active: Optional[int] = Field(default=None, ge=0, le=1)


def _conflict_or_404(e: ValueError) -> HTTPException:
    """Map ValueError → HTTP error: missing → 404, everything else → 409."""
    msg = str(e)
    if "不存在" in msg:
        return HTTPException(status_code=404, detail=msg)
    return HTTPException(status_code=409, detail=msg)


def _require_key() -> None:
    """Without FTA_MAILBOX_ENC_KEY, password writes are refused with a clear
    Chinese message. Sending is unaffected (env fallback)."""
    if not mailbox_manager.encryption_available():
        raise HTTPException(
            status_code=503,
            detail="未设置 FTA_MAILBOX_ENC_KEY，无法保存邮箱密码。"
                   "请在 .env 设置密钥后重试（发信仍走 env 凭证，不受影响）。",
        )


@router.get("")
async def get_mailboxes():
    """List mailboxes with today's quota state (password masked)."""
    conn = get_db()
    try:
        return {"mailboxes": [b.to_dict() for b in mailbox_manager.list_mailboxes(conn)]}
    finally:
        conn.close()


@router.post("")
async def create_mailbox(data: MailboxCreate):
    """Add a new SMTP account. Reserved names (default/zoho) are refused."""
    if data.password is not None:
        _require_key()
    conn = get_db()
    try:
        try:
            box = mailbox_manager.create_mailbox(
                conn,
                name=data.name,
                display_name=data.display_name,
                smtp_host=data.smtp_host,
                smtp_port=data.smtp_port,
                use_tls=data.use_tls,
                username=data.username,
                password=data.password,
                from_email=data.from_email,
                from_name=data.from_name,
                daily_limit=data.daily_limit,
            )
        except ValueError as e:
            raise _conflict_or_404(e) from e
        return box.to_dict()
    finally:
        conn.close()


@router.put("/{name}")
async def update_mailbox(name: str, data: MailboxUpdate):
    """Update a mailbox (only provided fields). 404 if missing."""
    if data.password is not None:
        _require_key()
    conn = get_db()
    try:
        try:
            box = mailbox_manager.update_mailbox(
                conn, name,
                display_name=data.display_name,
                smtp_host=data.smtp_host,
                smtp_port=data.smtp_port,
                use_tls=data.use_tls,
                username=data.username,
                password=data.password,
                from_email=data.from_email,
                from_name=data.from_name,
                daily_limit=data.daily_limit,
                is_active=data.is_active,
            )
        except ValueError as e:
            raise _conflict_or_404(e) from e
        if not box:
            raise HTTPException(status_code=404, detail=f"邮箱「{name}」不存在")
        return box.to_dict()
    finally:
        conn.close()


@router.patch("/{name}")
async def patch_mailbox(name: str, data: MailboxUpdate):
    """Alias of PUT (PATCH-friendly clients)."""
    return await update_mailbox(name, data)


@router.delete("/{name}")
async def delete_mailbox(name: str):
    """Delete a mailbox. 409 for reserved names or if it sent today."""
    conn = get_db()
    try:
        try:
            mailbox_manager.delete_mailbox(conn, name)
        except ValueError as e:
            raise _conflict_or_404(e) from e
        return {"deleted": True, "name": name}
    finally:
        conn.close()


@router.post("/{name}/test")
async def test_mailbox(name: str):
    """Connect + login to the mailbox's SMTP server. Sends NOTHING."""
    conn = get_db()
    try:
        # 不存在且非保留邮箱 → 404（绝不回退到默认邮箱配置，CRM-002）。
        # 保留邮箱 default/zoho 即使 DB 播种行缺失也可用各自 env profile。
        if name not in mailbox_manager.RESERVED_NAMES and not mailbox_manager.get_mailbox_row(name, conn):
            raise HTTPException(status_code=404, detail=f"邮箱「{name}」不存在")
        return mailbox_manager.test_mailbox(conn, name)
    finally:
        conn.close()
