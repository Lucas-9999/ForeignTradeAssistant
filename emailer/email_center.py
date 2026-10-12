"""M6 Email Center orchestration.

This module adds UI-facing safety around the canonical ``send_batch`` core:

* preview is read-only and binds an exact list of lead IDs;
* execution requires a short-lived, one-time confirmation plus operator token;
* one UI job per mailbox may run at a time;
* actual sending still goes through ``emailer.batch.send_batch``;
* a metadata-only JSONL audit is written beside the active database.

No SMTP call is made by preview/options/status functions.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import config
from crm import mailbox_manager
from emailer.batch import (
    ZOHO_FUMENG_SOURCES,
    _eligible_leads_query,
    get_batch_status,
    send_batch as core_send_batch,
)
from emailer.segmentation import SegmentMatcher
from emailer.templates import TemplateLoader

MAX_BATCH_SIZE = 50
PREVIEW_TTL_MINUTES = 10

_lock = threading.RLock()
_previews: dict[str, dict] = {}
_jobs: dict[str, dict] = {}
_active_mailboxes: set[str] = set()
logger = logging.getLogger(__name__)


class EmailCenterError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def send_is_enabled() -> bool:
    return bool(os.getenv("FTA_UI_SEND_TOKEN", "").strip())


def require_send_token(token: Optional[str]) -> None:
    expected = os.getenv("FTA_UI_SEND_TOKEN", "").strip()
    if not expected:
        raise EmailCenterError(
            503,
            "尚未配置 FTA_UI_SEND_TOKEN，Web 发信保持关闭；CLI 与 scheduler 不受影响。",
        )
    if not token or not secrets.compare_digest(token, expected):
        raise EmailCenterError(403, "发信授权码无效")


def _audit_path() -> Path:
    # Following DB_PATH keeps tests isolated automatically because conftest
    # redirects DB_PATH to each test's temporary directory.
    return Path(config.DB_PATH).parent / "email_center_audit.jsonl"


def _write_audit(event: str, **metadata) -> None:
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": _iso(), "event": event, **metadata}
    # Never accept arbitrary request bodies here: callers pass only a fixed
    # metadata allow-list (no token, password, body, recipient or subject).
    with _lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_legacy_api_event(
    event: str,
    route: str,
    requested: int,
    succeeded: Optional[bool] = None,
) -> None:
    """Audit legacy HTTP send entry points without recipient or message data."""
    metadata: dict = {"route": route, "requested": requested}
    if succeeded is not None:
        metadata["succeeded"] = succeeded
    _write_audit(event, **metadata)


def recent_audit(limit: int = 20) -> list[dict]:
    path = _audit_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # One interrupted append must not hide older valid audit records.
            continue
        if len(records) >= max(1, min(limit, 100)):
            break
    return records


def get_options(conn: sqlite3.Connection) -> dict:
    loader = TemplateLoader()
    matcher = SegmentMatcher()
    boxes = [
        box.to_dict()
        for box in mailbox_manager.list_mailboxes(conn)
        if box.is_active
    ]
    # MailboxInfo.to_dict() is already password-free. Keep only fields the
    # Email Center needs, reducing accidental contract expansion.
    mailbox_options = [
        {
            "name": b["name"],
            "display_name": b["display_name"],
            "remaining": b["remaining"],
            "daily_limit": b["daily_limit"],
            "credential_source": b["credential_source"],
            "has_smtp_password": b["has_smtp_password"],
        }
        for b in boxes
    ]
    return {
        "send_enabled": send_is_enabled(),
        "max_batch_size": MAX_BATCH_SIZE,
        "mailboxes": mailbox_options,
        "template_categories": loader.categories,
        "templates": [
            {
                "id": template.get("id"),
                "name": template.get("name") or template.get("id"),
                "category": template.get("category") or "general",
                "language": template.get("language") or "en",
            }
            for template in loader.templates
            if template.get("id")
        ],
        "country_groups": matcher.country_group_names(),
        "industry_groups": matcher.industry_group_names(),
    }


def _mailbox_or_error(conn: sqlite3.Connection, name: str):
    box = mailbox_manager.get_mailbox(conn, name)
    if not box:
        raise EmailCenterError(404, f"邮箱「{name}」不存在")
    if not box.is_active:
        raise EmailCenterError(409, f"邮箱「{name}」已停用")
    if not box.has_smtp_password:
        raise EmailCenterError(409, f"邮箱「{name}」尚未配置可用凭证")
    return box


def create_preview(conn: sqlite3.Connection, request: dict) -> dict:
    mailbox = str(request.get("mailbox") or "default")
    box = _mailbox_or_error(conn, mailbox)
    requested_limit = max(1, min(int(request.get("limit") or 1), MAX_BATCH_SIZE))
    effective_limit = min(requested_limit, max(0, int(box.remaining)))

    loader = TemplateLoader()
    category = request.get("category") or None
    if category and category not in loader.categories:
        raise EmailCenterError(400, f"模板分类「{category}」不存在")
    template_id = request.get("template_id") or None
    if not template_id:
        raise EmailCenterError(400, "请选择一个具体邮件模板后再预览")
    fixed_template = loader.get_by_id(template_id) if template_id else None
    if template_id and fixed_template is None:
        raise EmailCenterError(400, f"邮件模板「{template_id}」不存在")

    requested_industry_group = request.get("industry_group") or None
    template_industry_group = fixed_template.get("industry_group")
    if template_industry_group and template_industry_group != "general":
        if (
            requested_industry_group
            and requested_industry_group != template_industry_group
        ):
            raise EmailCenterError(
                400,
                "所选邮件模板与行业分组不一致，请更换模板或清除行业筛选",
            )
        # A fixed vertical template must not be rendered for a lead outside
        # that vertical.  This is enforced server-side so API callers cannot
        # bypass the Email Center's review flow.
        effective_industry_group = template_industry_group
    else:
        effective_industry_group = requested_industry_group

    if effective_limit == 0:
        return {
            "candidate_count": 0,
            "lead_ids": [],
            "samples": [],
            "confirmation_id": None,
            "confirmation_text": None,
            "expires_at": None,
            "message": "该邮箱今日剩余额度为 0",
        }

    source = request.get("source") or None
    # The preview freezes an exact recipient set for send_batch.  Keep its
    # source exclusions identical to send_batch so a reviewed recipient is
    # never silently removed before the SMTP stage.
    exclude_sources = list(config.OUTREACH_ARCHIVED_SOURCES)
    if mailbox == "default" and not source:
        exclude_sources.extend(ZOHO_FUMENG_SOURCES)
    exclude_sources = list(dict.fromkeys(exclude_sources)) or None

    query, params = _eligible_leads_query(
        limit=effective_limit,
        country_filter=request.get("country_filter") or None,
        country_group=request.get("country_group") or None,
        industry_group=effective_industry_group,
        include_low_relevance=bool(request.get("include_low_relevance")),
        lead_ids=request.get("lead_ids") or None,
        status="new",
        source=source,
        exclude_sources=exclude_sources,
        mx_overscan=1,
    )
    rows = conn.execute(query, params).fetchall()
    lead_ids = [int(row["id"]) for row in rows]

    samples = []
    for row in rows[:5]:
        lead = dict(row)
        template = fixed_template or (loader.pick(category) if category else loader.pick_for_lead(lead))
        if not template:
            continue
        rendered = loader.render_email(
            template,
            company_name=lead.get("company_name") or "your company",
            contact_name=lead.get("contact_name"),
            country=lead.get("country") or "",
            industry=lead.get("industry") or "industrial supply",
            source=lead.get("source") or "",
        )
        samples.append({
            "lead_id": int(lead["id"]),
            "company_name": lead.get("company_name"),
            "email": lead.get("email"),
            "subject": rendered["subject"],
            "body_html": rendered["body_html"],
            "template_id": template.get("id"),
            "template_name": template.get("name"),
        })

    if not lead_ids:
        return {
            "candidate_count": 0,
            "lead_ids": [],
            "samples": [],
            "confirmation_id": None,
            "confirmation_text": None,
            "expires_at": None,
            "message": "没有符合条件且尚未联系的线索",
        }

    confirmation_id = secrets.token_urlsafe(24)
    expires = _utc_now() + timedelta(minutes=PREVIEW_TTL_MINUTES)
    confirmation_text = f"确认发送 {len(lead_ids)} 封"
    normalized = {
        "mailbox": mailbox,
        "limit": len(lead_ids),
        "lead_ids": lead_ids,
        "country_filter": request.get("country_filter") or None,
        "country_group": request.get("country_group") or None,
        "industry_group": effective_industry_group,
        "include_low_relevance": bool(request.get("include_low_relevance")),
        "category": category,
        "template_id": template_id,
        "source": source,
        "exclude_sources": exclude_sources,
    }
    with _lock:
        expired = [key for key, item in _previews.items() if item["expires_at"] <= _utc_now()]
        for key in expired:
            _previews.pop(key, None)
        _previews[confirmation_id] = {
            "expires_at": expires,
            "confirmation_text": confirmation_text,
            "request": normalized,
        }
    _write_audit(
        "preview_created",
        mailbox=mailbox,
        candidate_count=len(lead_ids),
        category=category,
        template_id=template_id,
        country_group=normalized["country_group"],
        industry_group=normalized["industry_group"],
    )
    return {
        "candidate_count": len(lead_ids),
        "lead_ids": lead_ids,
        "samples": samples,
        "confirmation_id": confirmation_id,
        "confirmation_text": confirmation_text,
        "expires_at": _iso(expires),
        "message": "预览已生成；实际发送前仍会执行 MX、黑名单和额度检查。",
    }


def _run_job(job_id: str, request: dict) -> None:
    mailbox = request["mailbox"]
    try:
        with _lock:
            _jobs[job_id]["status"] = "running"
        result = core_send_batch(
            limit=request["limit"],
            dry_run=False,
            country_filter=request["country_filter"],
            country_group=request["country_group"],
            industry_group=request["industry_group"],
            include_low_relevance=request["include_low_relevance"],
            category=request["category"],
            template_id=request["template_id"],
            lead_ids=request["lead_ids"],
            status="new",
            source=request["source"],
            exclude_sources=request["exclude_sources"],
            smtp_profile=mailbox,
            mx_prefilter=True,
        )
        with _lock:
            job = _jobs[job_id]
            job.update(
                status="completed",
                completed_at=_iso(),
                sent=int(result.get("sent", 0)),
                skipped=int(result.get("skipped", 0)),
                errors=int(result.get("errors", 0)),
                # Do not expose per-recipient addresses or raw SMTP errors
                # through the unauthenticated status endpoint.
                details=[],
            )
        _write_audit(
            "send_completed",
            job_id=job_id,
            mailbox=mailbox,
            requested=request["limit"],
            sent=int(result.get("sent", 0)),
            skipped=int(result.get("skipped", 0)),
            errors=int(result.get("errors", 0)),
        )
    except Exception as exc:
        logger.exception("Email Center job %s failed", job_id)
        with _lock:
            job = _jobs[job_id]
            job.update(
                status="failed",
                completed_at=_iso(),
                error="发送任务失败，请查看服务器日志和审计记录",
            )
        _write_audit("send_failed", job_id=job_id, mailbox=mailbox, error_type=type(exc).__name__)
    finally:
        with _lock:
            _active_mailboxes.discard(mailbox)


def start_send(
    conn: sqlite3.Connection,
    confirmation_id: str,
    confirmation_text: str,
    token: Optional[str],
) -> dict:
    require_send_token(token)
    now = _utc_now()
    with _lock:
        preview = _previews.get(confirmation_id)
        if not preview:
            raise EmailCenterError(409, "确认记录不存在、已使用或已失效，请重新预览")
        if preview["expires_at"] <= now:
            _previews.pop(confirmation_id, None)
            raise EmailCenterError(409, "确认记录已过期，请重新预览")
        if confirmation_text != preview["confirmation_text"]:
            raise EmailCenterError(409, "确认文字不匹配")

        request = preview["request"]
        mailbox = request["mailbox"]
        # Configuration can change during the preview TTL. Revalidate before
        # consuming the one-time confirmation and starting a real send.
        _mailbox_or_error(conn, mailbox)
        if mailbox in _active_mailboxes or get_batch_status(mailbox).get("running"):
            raise EmailCenterError(409, f"邮箱「{mailbox}」已有发送任务运行中")

        # One-time confirmation is consumed before starting the thread, so a
        # double click/retry cannot enqueue a second send.
        _previews.pop(confirmation_id, None)
        _active_mailboxes.add(mailbox)
        job_id = secrets.token_urlsafe(18)
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "mailbox": mailbox,
            "requested": request["limit"],
            "sent": 0,
            "skipped": 0,
            "errors": 0,
            "started_at": _iso(),
            "completed_at": None,
            "error": None,
            "details": [],
        }

    _write_audit("send_started", job_id=job_id, mailbox=mailbox, requested=request["limit"])
    thread = threading.Thread(target=_run_job, args=(job_id, request), daemon=True)
    thread.start()
    return {"job_id": job_id, "status": "queued"}


def get_job(job_id: str) -> Optional[dict]:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None
