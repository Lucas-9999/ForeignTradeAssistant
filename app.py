"""
FTA API — FastAPI application for Foreign Trade Assistant.

Endpoints (Phase 1):
- GET    /api/v1/health                        — Health check
- POST   /api/v1/leads                         — Create lead
- GET    /api/v1/leads                         — List leads (filterable)
- GET    /api/v1/leads/{id}                    — Get lead detail
- PUT    /api/v1/leads/{id}                    — Update lead
- DELETE /api/v1/leads/{id}                    — Delete lead
- POST   /api/v1/leads/{id}/promote            — Promote lead → customer
- GET    /api/v1/customers                     — List customers
- GET    /api/v1/customers/{id}                — Get customer + comm history
- GET    /api/v1/customers/due-followup        — Customers due for follow-up
- POST   /api/v1/emails/send                  — Send single email
- POST   /api/v1/emails/send-batch            — Send batch with rotation
- POST   /api/v1/emails/test                  — Preview rendered template
- GET    /api/v1/email-center/options         — M6 send options (read-only)
- POST   /api/v1/email-center/preview         — M6 freeze recipients + preview
- POST   /api/v1/email-center/send            — M6 authorized one-time send
- GET    /api/v1/email-center/status/{id}     — M6 background job status
- GET    /api/v1/email-center/audit           — M6 metadata-only audit trail
- GET    /api/v1/emails/templates             — List email templates
- POST   /api/v1/emails/templates             — Create template
- PUT    /api/v1/emails/templates/{id}        — Update template
- DELETE /api/v1/emails/templates/{id}        — Delete template
- GET    /api/v1/antispam/status              — Anti-spam quota status
- GET    /api/v1/collector/status             — Collector scheduler status
- GET    /api/v1/collector/sources            — List available data sources
- GET    /api/v1/collector/tasks              — Collection task history
- POST   /api/v1/collector/run                — Trigger manual collection
"""

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

import config
from crm.db import build_database, ensure_schema_current, get_connection
from crm.manager import (
    LeadManager,
    CustomerManager,
    TemplateManager,
    CommunicationManager,
    BlacklistManager,
)
from crm.models import (
    Lead,
    Customer,
    EmailTemplate,
    Communication,
)
from emailer.templates import EmailTemplateRenderer
from emailer.rotation import AntiSpamGuard, compute_send_delay
from emailer.batch import send_batch as core_send_batch
from emailer.send_core import SendIntent, send_one_async
from services.email_reputation.sending_guard import build_sending_guard
from emailer import email_center as email_center_service
from crm.lead_query import query_leads
from services.email_reputation.diagnostic_safety import sanitize_diagnostic

# ── Setup ──────────────────────────────────────────────────────────────────

# Ensure log directory exists
config.LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format=config.LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            config.LOG_DIR / "fta.log",
            maxBytes=5 * 1024 * 1024,  # 5 MB per file
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("fta.api")

# ── Lifespan (replaces deprecated on_event) ──────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the schema gate and, unless explicitly disabled, the scheduler."""
    # ── CRM-004：schema 版本落后 → 拒绝启动（显式迁移，绝不自动改生产库）──
    ensure_schema_current(config.DB_PATH)
    # ── Startup ────────────────────────────────────────────────────────
    scheduler_started = False
    if getattr(app.state, "scheduler_enabled", True):
        try:
            from collector.scheduler import start_scheduler
            from collector.cli import SOURCE_REGISTRY
            scheduler_work_enabled = bool(
                SOURCE_REGISTRY
                or config.IMAP_CHECK_ENABLED
                or config.EMAIL_SCHEDULE_ENABLED
                or config.ZOHO_BATCH_ENABLED
                or config.FOLLOWUP_SCHEDULE_ENABLED
            )
            if scheduler_work_enabled:
                start_scheduler()
                scheduler_started = True
                logger.info(
                    "Scheduler started with sources=%s imap_check=%s "
                    "email_schedule=%s zoho_batch=%s",
                    list(SOURCE_REGISTRY.keys()),
                    config.IMAP_CHECK_ENABLED,
                    config.EMAIL_SCHEDULE_ENABLED,
                    config.ZOHO_BATCH_ENABLED,
                )
            else:
                logger.info("No collection or email scheduler work enabled — scheduler idle")
        except Exception:
            logger.exception("Failed to start collection scheduler")
    else:
        logger.info("CRM-only server mode: collection and email schedulers are disabled")

    yield  # ── App runs here ────────────────────────────────────────────

    # ── Shutdown ───────────────────────────────────────────────────────
    if scheduler_started:
        try:
            from collector.scheduler import stop_scheduler
            stop_scheduler(wait=False)
        except Exception:
            pass


app = FastAPI(
    title="Foreign Trade Assistant API",
    description="规则驱动的客户工作流与邮件安全控制",
    version="0.2.0",
    lifespan=lifespan,
)

DB_PATH = config.DB_PATH
renderer = EmailTemplateRenderer()


# ── Mount dashboard ────────────────────────────────────────────────────────

try:
    from collector.dashboard import router as dashboard_router
    # Manually register routes (include_router has issues in some FastAPI versions)
    for route in dashboard_router.routes:
        app.router.routes.append(route)
    logger.info(f"Collection dashboard mounted at /dashboard/ ({len(dashboard_router.routes)} routes)")
except ImportError:
    pass

# ── CRM Web Console routers (M1: read-only) ────────────────────────────────
from api import dashboard as dashboard_api
from api import mailboxes as mailboxes_api
from api import tags as tags_api
from api import groups as groups_api
from api import email_center as email_center_api
from api import reputation as reputation_api
app.include_router(dashboard_api.router)
app.include_router(mailboxes_api.router)
app.include_router(tags_api.router)
app.include_router(groups_api.router)
app.include_router(email_center_api.router)
app.include_router(reputation_api.router)

# ── Chinese CRM Web Console (M2: SPA) ───────────────────────────────────────
# Mounted at /ui/ (NOT / — the collector dashboard owns /dashboard/*). SPA
# fallback serves index.html for client-side routes so /ui/leads etc. work on
# deep-link/refresh. Skips mounting if frontend hasn't been built yet (dev).
try:
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    _FRONTEND_DIST = config.BASE_DIR / "frontend" / "dist"

    class SPAStaticFiles(StaticFiles):
        """Serve built assets; fall back to index.html for any unknown path."""

        async def get_response(self, path: str, scope):
            try:
                return await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                # Starlette 1.3.1 raises (not returns) 404 for a missing file,
                # and it's the BASE starlette.exceptions.HTTPException, not the
                # fastapi.HTTPException subclass — catch the base class.
                if exc.status_code != 404:
                    raise
                index_path = self.directory / "index.html"
                if index_path.exists():
                    return FileResponse(index_path)
                raise

    if _FRONTEND_DIST.exists():
        app.mount("/ui", SPAStaticFiles(directory=_FRONTEND_DIST, html=True), name="ui")
        logger.info(f"CRM Web Console mounted at /ui/ ({_FRONTEND_DIST})")
    else:
        logger.warning(f"Frontend not built yet — /ui/ disabled (run `npm run build` in frontend/)")
except Exception as _ui_err:  # pragma: no cover
    logger.warning(f"Could not mount /ui/: {_ui_err}")


def get_db() -> sqlite3.Connection:
    """Get a database connection, building the DB if it doesn't exist.

    Reads config.DB_PATH at call time (not the module-level capture) so a
    runtime redirect (tests, future mailbox env-seeding) is always honored.
    """
    if not config.DB_PATH.exists():
        logger.info(f"Database not found, building at {config.DB_PATH}")
        build_database(config.DB_PATH)
    return get_connection(config.DB_PATH)


# ═══════════════════════════════════════════════════════════════════════════
# Pydantic schemas
# ═══════════════════════════════════════════════════════════════════════════


class LeadCreate(BaseModel):
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    website: Optional[str] = None
    source: str = "manual"
    source_url: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None
    notes: Optional[str] = None


class LeadUpdate(BaseModel):
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    website: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class CustomerCreate(BaseModel):
    company_name: str
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    country: Optional[str] = None
    industry: Optional[str] = None
    preferred_channel: str = "email"
    followup_interval_days: int = 30
    notes: Optional[str] = None
    tags: Optional[str] = None


class CustomerUpdate(BaseModel):
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    country: Optional[str] = None
    industry: Optional[str] = None
    preferred_channel: Optional[str] = None
    followup_interval_days: Optional[int] = None
    notes: Optional[str] = None
    tags: Optional[str] = None
    is_active: Optional[int] = None


class EmailSendRequest(BaseModel):
    lead_id: Optional[int] = None
    customer_id: Optional[int] = None
    template_id: Optional[int] = None
    custom_subject: Optional[str] = None
    custom_body: Optional[str] = None


class EmailBatchRequest(BaseModel):
    lead_ids: list[int] = []
    template_id: Optional[int] = None
    category: Optional[str] = None
    country_group: Optional[str] = None
    industry_group: Optional[str] = None
    include_low_relevance: bool = False
    limit: int = 20
    dry_run: bool = False


class EmailTestRequest(BaseModel):
    template_id: Optional[int] = None
    subject_template: Optional[str] = None
    body_template: Optional[str] = None


class TemplateCreate(BaseModel):
    name: str
    subject_template: str
    body_template: str
    description: Optional[str] = None
    category: str = "icebreaker"
    language: str = "en"


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    subject_template: Optional[str] = None
    body_template: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    language: Optional[str] = None
    is_active: Optional[int] = None


# ═══════════════════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/health")
async def health_check():
    """Health check — returns DB status and counts."""
    conn = None
    try:
        conn = get_db()
        lead_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        customer_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        template_count = conn.execute("SELECT COUNT(*) FROM email_templates WHERE is_active=1").fetchone()[0]
        # All-time total from communications
        total_sent = conn.execute(
            "SELECT COUNT(*) FROM communications WHERE status='sent'"
        ).fetchone()[0]

        guard = AntiSpamGuard(conn)
        quota = guard.get_status()

        return {
            "status": "ok",
            "database": str(DB_PATH),
            "leads": lead_count,
            "customers": customer_count,
            "active_templates": template_count,
            "emails_sent_today": quota["emails_sent_today"],
            "emails_sent_total": total_sent,
            "emails_remaining": quota["remaining"],
        }
    except FileNotFoundError:
        return {"status": "no_database", "database": str(DB_PATH), "leads": 0, "customers": 0}
    finally:
        if conn is not None:
            conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Leads
# ═══════════════════════════════════════════════════════════════════════════


@app.post("/api/v1/leads")
async def create_lead(data: LeadCreate):
    """Create a new lead (manual entry)."""
    conn = get_db()
    try:
        mgr = LeadManager(conn)
        lead = Lead(**data.model_dump())
        lead = mgr.create(lead)
        return lead.to_dict()
    finally:
        conn.close()


@app.get("/api/v1/leads")
async def list_leads(
    status: Optional[str] = Query(None, description="Filter by status"),
    source: Optional[str] = Query(None, description="Filter by source"),
    search: Optional[str] = Query(None, description="Search company/contact/email"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    # ── CRM Web Console paginated mode (M1, additive) ─────────────────
    page: Optional[int] = Query(None, ge=1,
                                description="Page number. Omit for legacy {leads, total_by_status} mode"),
    page_size: int = Query(50, ge=1, le=200),
    country: Optional[str] = Query(None, description="Filter by country"),
    tag: Optional[list[str]] = Query(None, description="Filter by tag name(s) — AND (needs schema V7)"),
    has_sent: Optional[bool] = Query(None, description="Only leads already emailed"),
    has_replied: Optional[bool] = Query(None, description="Only leads that replied"),
    sort: Optional[str] = Query(None, description="Sort key from whitelist; '-' prefix = DESC"),
):
    """List leads with optional filters.

    Two modes (additive — legacy callers unchanged):
      - no `page`: legacy {leads, total_by_status} via LeadManager.list_all
      - with `page`: paginated {items, total, page, page_size, total_pages} with
        has_sent/has_replied/tags derived from communications (see crm.lead_query)
    """
    conn = get_db()
    try:
        mgr = LeadManager(conn)
        status_counts = mgr.count_by_status()

        if page is not None:
            items, total = query_leads(
                conn,
                page=page, page_size=page_size,
                search=search, country=country, source=source, status=status,
                tags=tag, has_sent=has_sent, has_replied=has_replied, sort=sort,
            )
            total_pages = (total + page_size - 1) // page_size if total else 0
            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "total_by_status": status_counts,
            }

        leads = mgr.list_all(status=status, source=source, search=search, offset=offset, limit=limit)
        return {
            "leads": [l.to_dict() for l in leads],
            "total_by_status": status_counts,
        }
    finally:
        conn.close()


@app.get("/api/v1/leads/{lead_id}")
async def get_lead(lead_id: int):
    """Get a single lead by ID."""
    conn = get_db()
    try:
        mgr = LeadManager(conn)
        lead = mgr.get_by_id(lead_id)
        if not lead:
            raise HTTPException(status_code=404, detail=f"Lead #{lead_id} not found")
        return lead.to_dict()
    finally:
        conn.close()


@app.get("/api/v1/leads/{lead_id}/communications")
async def get_lead_communications(lead_id: int, limit: int = Query(50, ge=1, le=200)):
    """Contact timeline for a lead (M3 detail drawer)."""
    conn = get_db()
    try:
        if not LeadManager(conn).get_by_id(lead_id):
            raise HTTPException(status_code=404, detail=f"Lead #{lead_id} not found")
        comms = CommunicationManager(conn).get_by_lead(lead_id, limit=limit)
        return {"lead_id": lead_id, "communications": comms}
    finally:
        conn.close()


@app.put("/api/v1/leads/{lead_id}")
async def update_lead(lead_id: int, data: LeadUpdate):
    """Update a lead."""
    conn = get_db()
    try:
        mgr = LeadManager(conn)
        lead = mgr.get_by_id(lead_id)
        if not lead:
            raise HTTPException(status_code=404, detail=f"Lead #{lead_id} not found")

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(lead, key, value)

        mgr.update(lead)
        return lead.to_dict()
    finally:
        conn.close()


@app.delete("/api/v1/leads/{lead_id}")
async def delete_lead(lead_id: int):
    """Delete a lead and its communications."""
    conn = get_db()
    try:
        mgr = LeadManager(conn)
        if not mgr.get_by_id(lead_id):
            raise HTTPException(status_code=404, detail=f"Lead #{lead_id} not found")
        mgr.delete(lead_id)
        return {"deleted": True, "lead_id": lead_id}
    finally:
        conn.close()


@app.post("/api/v1/leads/{lead_id}/promote")
async def promote_lead(lead_id: int):
    """Promote a lead to customer status."""
    conn = get_db()
    try:
        mgr = LeadManager(conn)
        customer_id = mgr.promote_to_customer(lead_id)
        if customer_id is None:
            raise HTTPException(status_code=404, detail=f"Lead #{lead_id} not found")
        return {"promoted": True, "lead_id": lead_id, "customer_id": customer_id}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Customers
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/customers")
async def list_customers(
    search: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List customers."""
    conn = get_db()
    try:
        mgr = CustomerManager(conn)
        customers = mgr.list_all(search=search, is_active=1, offset=offset, limit=limit)
        return {"customers": [c.to_dict() for c in customers]}
    finally:
        conn.close()


@app.get("/api/v1/customers/{customer_id}")
async def get_customer(customer_id: int):
    """Get a customer with communication history."""
    conn = get_db()
    try:
        mgr = CustomerManager(conn)
        customer = mgr.get_by_id(customer_id)
        if not customer:
            raise HTTPException(status_code=404, detail=f"Customer #{customer_id} not found")
        history = mgr.get_communication_history(customer_id)
        return {
            "customer": customer.to_dict(),
            "communications": history,
        }
    finally:
        conn.close()


@app.get("/api/v1/customers/due-followup")
async def get_due_followups(days_ahead: int = Query(7, ge=1, le=90)):
    """Get customers whose follow-up is due within N days."""
    conn = get_db()
    try:
        mgr = CustomerManager(conn)
        due = mgr.get_due_followups(days_ahead=days_ahead)
        overdue = mgr.get_overdue_followups()
        return {
            "due_soon": [c.to_dict() for c in due],
            "overdue": [c.to_dict() for c in overdue],
        }
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Email
# ═══════════════════════════════════════════════════════════════════════════


@app.post("/api/v1/emails/send")
async def email_send(
    data: EmailSendRequest,
    x_fta_send_token: Optional[str] = Header(default=None, alias="X-FTA-Send-Token"),
):
    """Send a single email to a lead or customer."""
    try:
        email_center_service.require_send_token(x_fta_send_token)
    except email_center_service.EmailCenterError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    email_center_service.record_legacy_api_event(
        "legacy_send_started",
        "/api/v1/emails/send",
        1,
    )
    conn = get_db()
    try:
        guard = AntiSpamGuard(conn)
        sending_guard = build_sending_guard(
            conn, mailbox="default", anti_spam_guard=guard
        )
        lead_mgr = LeadManager(conn)
        cust_mgr = CustomerManager(conn)
        tmpl_mgr = TemplateManager(conn)
        comm_mgr = CommunicationManager(conn)

        # ── Resolve recipient ──
        email = None
        company_name = None
        contact_name = None
        country = None
        industry = None
        lead_id = data.lead_id

        if data.customer_id:
            cust = cust_mgr.get_by_id(data.customer_id)
            if not cust:
                raise HTTPException(status_code=404, detail=f"Customer #{data.customer_id} not found")
            email = cust.email
            company_name = cust.company_name
            contact_name = cust.contact_name
            country = cust.country
            industry = cust.industry
            lead_id = cust.lead_id
        elif data.lead_id:
            lead = lead_mgr.get_by_id(data.lead_id)
            if not lead:
                raise HTTPException(status_code=404, detail=f"Lead #{data.lead_id} not found")
            email = lead.email
            company_name = lead.company_name
            contact_name = lead.contact_name
            country = lead.country
            industry = lead.industry
        else:
            raise HTTPException(status_code=400, detail="Must provide lead_id or customer_id")

        if not email:
            raise HTTPException(status_code=400, detail="Recipient has no email address")

        # ── Duplicate send check: has this lead ever been contacted? ──
        if lead_id and comm_mgr.has_outbound_to(lead_id):
            raise HTTPException(
                status_code=409,
                detail=f"Lead #{lead_id} has already received a first-contact email. "
                       f"Use a follow-up template instead.",
            )

        # ── Resolve template ──
        if data.custom_subject and data.custom_body:
            subject = data.custom_subject
            body = data.custom_body
            template_id = None
        elif data.template_id:
            tmpl = tmpl_mgr.get_by_id(data.template_id)
            if not tmpl:
                raise HTTPException(status_code=404, detail=f"Template #{data.template_id} not found")
            subject = renderer.render_subject(
                tmpl.subject_template,
                company_name=company_name,
                contact_name=contact_name,
                country=country,
                industry=industry,
            )
            body = renderer.render_body(
                tmpl.body_template,
                company_name=company_name,
                contact_name=contact_name,
                country=country,
                industry=industry,
            )
            template_id = tmpl.id
        else:
            # Auto-select via rotation
            tmpl = tmpl_mgr.select_for_rotation("icebreaker")
            if not tmpl:
                raise HTTPException(status_code=400, detail="No active templates available")
            subject = renderer.render_subject(
                tmpl.subject_template,
                company_name=company_name,
                contact_name=contact_name,
                country=country,
                industry=industry,
            )
            body = renderer.render_body(
                tmpl.body_template,
                company_name=company_name,
                contact_name=contact_name,
                country=country,
                industry=industry,
            )
            template_id = tmpl.id

        # ── Send through the canonical guarded seam ────────────────────
        # send_one_async evaluates + records the SendingGuard decision and only
        # reaches SMTP on ALLOW. A BLOCK / DELAY / PAUSE_MAILBOX decision
        # surfaces as HTTP 429 (matching the legacy can_send rejection).
        outcome = await send_one_async(
            SendIntent(
                email=email,
                subject=subject,
                body_html=body,
                to_name=contact_name,
                lead_id=lead_id,
                customer_id=data.customer_id,
                template_id=template_id,
                source="legacy_api",
            ),
            sending_guard=sending_guard,
        )
        if outcome.blocked:
            raise HTTPException(status_code=429, detail=outcome.message)
        if outcome.dry_run:
            # P10 simulated the ALLOW decision at the canonical seam.  Do not
            # write a communication, quota use, lead/contact state or bounce.
            message = sanitize_diagnostic(outcome.message)
            email_center_service.record_legacy_api_event(
                "legacy_send_dry_run",
                "/api/v1/emails/send",
                1,
                False,
            )
            return {
                "success": False,
                "dry_run": True,
                "message": message,
                "communication_id": None,
                "template_used": template_id,
            }
        success, message, msg_id = outcome.success, outcome.message, outcome.message_id
        is_hard_bounce = False
        if not success:
            is_hard_bounce = guard.record_smtp_failure(
                email,
                message,
                provider="default",
            )
        message = sanitize_diagnostic(message)

        # ── Record communication ──
        comm = Communication(
            lead_id=lead_id,
            customer_id=data.customer_id,
            template_id=template_id,
            channel="email",
            direction="outbound",
            subject=subject,
            body=body,
            status="sent" if success else "failed",
            error_message=None if success else message,
            message_id=msg_id,
        )
        comm = comm_mgr.log(comm)

        # ── Update state ──
        if success:
            guard.record_send(email)
            if template_id:
                tmpl_mgr.record_usage(template_id)
            if lead_id:
                lead_mgr.mark_contacted(lead_id)
            if data.customer_id:
                cust_mgr.record_contact(data.customer_id)
        else:
            if is_hard_bounce:
                if lead_id:
                    lead_mgr.mark_dead(lead_id)

        response = {
            "success": success,
            "message": message,
            "communication_id": comm.id,
            "template_used": template_id,
        }
        email_center_service.record_legacy_api_event(
            "legacy_send_completed",
            "/api/v1/emails/send",
            1,
            success,
        )
        return response
    except Exception:
        email_center_service.record_legacy_api_event(
            "legacy_send_failed",
            "/api/v1/emails/send",
            1,
            False,
        )
        raise
    finally:
        conn.close()


@app.post("/api/v1/emails/send-batch")
async def email_send_batch(
    data: EmailBatchRequest,
    x_fta_send_token: Optional[str] = Header(default=None, alias="X-FTA-Send-Token"),
):
    """Send emails to a batch of leads with per-lead template matching.

    Delegates to the consolidated emailer.batch.send_batch core so the API,
    Streamlit UI, and CLI share one implementation (anti-spam, rotation,
    segment filters, priority ordering).
    """
    # Read-only dry-run remains available for operational diagnostics. Any
    # real send is fail-closed behind the same operator token as M6.
    if not data.dry_run:
        try:
            email_center_service.require_send_token(x_fta_send_token)
        except email_center_service.EmailCenterError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        email_center_service.record_legacy_api_event(
            "legacy_send_started",
            "/api/v1/emails/send-batch",
            data.limit,
        )

    try:
        result = await asyncio.to_thread(
            core_send_batch,
            limit=data.limit,
            dry_run=data.dry_run,
            country_group=data.country_group,
            industry_group=data.industry_group,
            include_low_relevance=data.include_low_relevance,
            category=data.category,
            lead_ids=data.lead_ids or None,
        )
    except Exception:
        if not data.dry_run:
            email_center_service.record_legacy_api_event(
                "legacy_send_failed",
                "/api/v1/emails/send-batch",
                data.limit,
                False,
            )
        raise
    if not data.dry_run:
        email_center_service.record_legacy_api_event(
            "legacy_send_completed",
            "/api/v1/emails/send-batch",
            data.limit,
            result.get("errors", 0) == 0,
        )
    blocked = [
        d for d in result.get("details", []) if d.get("status") == "skipped"
    ]
    return {
        "sent": result.get("sent", 0),
        "failed": result.get("errors", 0),
        "blocked": len(blocked),
        "skipped": result.get("skipped", 0),
        "results": result.get("details", []),
        "blocked_list": blocked,
    }


@app.post("/api/v1/emails/test")
async def email_test(data: EmailTestRequest):
    """Preview a rendered template with sample data."""
    conn = get_db()
    try:
        if data.template_id:
            tmpl_mgr = TemplateManager(conn)
            tmpl = tmpl_mgr.get_by_id(data.template_id)
            if not tmpl:
                raise HTTPException(status_code=404, detail=f"Template #{data.template_id} not found")
            subject_tmpl = tmpl.subject_template
            body_tmpl = tmpl.body_template
        elif data.subject_template and data.body_template:
            subject_tmpl = data.subject_template
            body_tmpl = data.body_template
        else:
            raise HTTPException(status_code=400, detail="Provide template_id or subject_template+body_template")

        return renderer.preview(subject_tmpl, body_tmpl)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Templates
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/emails/templates")
async def list_templates(category: Optional[str] = Query(None)):
    """List active email templates."""
    conn = get_db()
    try:
        mgr = TemplateManager(conn)
        templates = mgr.list_active(category=category)
        return {"templates": [t.to_dict() for t in templates]}
    finally:
        conn.close()


@app.post("/api/v1/emails/templates")
async def create_template(data: TemplateCreate):
    """Create a new email template."""
    conn = get_db()
    try:
        mgr = TemplateManager(conn)
        tmpl = EmailTemplate(**data.model_dump())
        tmpl = mgr.create(tmpl)
        return tmpl.to_dict()
    finally:
        conn.close()


@app.put("/api/v1/emails/templates/{template_id}")
async def update_template(template_id: int, data: TemplateUpdate):
    """Update an email template."""
    conn = get_db()
    try:
        mgr = TemplateManager(conn)
        tmpl = mgr.get_by_id(template_id)
        if not tmpl:
            raise HTTPException(status_code=404, detail=f"Template #{template_id} not found")

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(tmpl, key, value)

        mgr.update(tmpl)
        return tmpl.to_dict()
    finally:
        conn.close()


@app.delete("/api/v1/emails/templates/{template_id}")
async def delete_template(template_id: int):
    """Delete an email template."""
    conn = get_db()
    try:
        mgr = TemplateManager(conn)
        if not mgr.get_by_id(template_id):
            raise HTTPException(status_code=404, detail=f"Template #{template_id} not found")
        mgr.delete(template_id)
        return {"deleted": True, "template_id": template_id}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Anti-Spam
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/antispam/status")
async def antispam_status():
    """Get current anti-spam quota status."""
    conn = get_db()
    try:
        guard = AntiSpamGuard(conn)
        return guard.get_status()
    finally:
        conn.close()




# ═══════════════════════════════════════════════════════════════════════════
# Collector — data collection endpoints
# ═══════════════════════════════════════════════════════════════════════════


class CollectRunRequest(BaseModel):
    sources: Optional[list[str]] = None  # None = all registered
    dry_run: bool = False


@app.get("/api/v1/collector/status")
async def collector_status():
    """Get collection scheduler status and recent task history."""
    try:
        from collector.scheduler import get_scheduler_status
        return get_scheduler_status()
    except ImportError:
        return {"running": False, "error": "Collector module not loaded"}


@app.get("/api/v1/collector/sources")
async def list_collector_sources():
    """List available data sources."""
    from collector.cli import SOURCE_REGISTRY
    return {"sources": list(SOURCE_REGISTRY.keys())}


@app.get("/api/v1/collector/tasks")
async def list_collection_tasks(days: int = Query(7, ge=1, le=90), limit: int = Query(50, ge=1, le=200)):
    """Get collection task history."""
    from collector.scheduler import get_recent_tasks
    return {"tasks": get_recent_tasks(days=days, limit=limit)}


@app.post("/api/v1/collector/run")
async def trigger_collection(data: CollectRunRequest = CollectRunRequest()):
    """Trigger a manual collection run (synchronous — may take a while)."""
    import asyncio as aio_mod

    from collector.cli import run_collection

    logger.info(f"Manual collection triggered: sources={data.sources}, dry_run={data.dry_run}")
    try:
        results = await run_collection(sources=data.sources, dry_run=data.dry_run)
    except Exception as e:
        logger.exception("Manual collection failed")
        raise HTTPException(status_code=500, detail=str(e))

    # Log results to scraping_tasks
    from collector.scheduler import _log_task_start, _log_task_complete
    for source, result in results.items():
        task_id = _log_task_start(source)
        error_msg = "Collection error" if result.get("error") else None
        _log_task_complete(
            task_id=task_id,
            leads_found=result.get("total_raw", 0),
            leads_new=result.get("created", 0),
            error=error_msg,
        )

    # Summary
    total_created = sum(r.get("created", 0) for r in results.values())
    return {
        "triggered": data.sources or list(results.keys()),
        "dry_run": data.dry_run,
        "total_created": total_created,
        "results": results,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Inbox — IMAP reply monitoring
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/inbox/recent")
async def inbox_recent(hours: int = Query(24, ge=1, le=168)):
    """Get recent customer replies detected by inbox monitoring."""
    conn = get_db()
    try:
        from crm.manager import CommunicationManager
        comm_mgr = CommunicationManager(conn)
        rows = conn.execute(
            """SELECT * FROM communications
               WHERE direction='inbound'
               ORDER BY sent_at DESC
               LIMIT ?""",
            (50,),
        ).fetchall()
        return {"replies": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/v1/inbox/check")
async def inbox_check(hours_back: int = Query(24, ge=1, le=168)):
    """Manually trigger an inbox check for customer replies.

    Searches IMAP inbox for messages received in the last `hours_back` hours,
    classifies them (reply/bounce/auto-reply), and records matches.
    """
    conn = get_db()
    try:
        from emailer.inbox import check_inbox_now
        result = check_inbox_now(conn, hours_back=hours_back)
        return result.to_dict()
    except Exception as e:
        logger.exception("Manual inbox check failed")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    # reload=False（生产）：reload 模式每有文件变更就重启 worker，多个 worker
    # 各自注册一批定时任务（08-13/14/17 双批次、重复 cron 的根因之一），且
    # 不重启 in-flight 的 APScheduler 线程会导致退出挂起。开发调试请显式
    # `uvicorn app:app --reload`。
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
