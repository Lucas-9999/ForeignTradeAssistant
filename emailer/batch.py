"""
Batch Email Sender — walks through leads and sends rotation emails.

Anti-spam enforcement:
  - Checks AntiSpamGuard before each send
  - Uses randomized inter-send delay (configurable)
  - Rotates templates to prevent spam pattern detection
  - Records sends to communications table
  - Auto-stops when daily cap is reached

Usage:
    python -m emailer send --category=general --limit=20
    python -m emailer send --category=custom --limit=10 --dry-run
    python -m emailer preview
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

import config
from services.email_reputation.diagnostic_safety import sanitize_diagnostic
from services.email_reputation.sending_guard import (
    GuardAction,
    GuardReasonCode,
    build_sending_guard,
)

logger = logging.getLogger("emailer.batch")

# SendingGuard reason codes whose decision stops the whole batch run
# (mailbox-level conditions) — matches the legacy "Daily limit reached → stop"
# behavior. Suppression / MX / per-domain limits only skip that lead and let
# the batch continue.
_BATCH_STOP_REASON_CODES = frozenset({
    GuardReasonCode.DAILY_QUOTA_EXCEEDED,
    GuardReasonCode.MAILBOX_PAUSED,
    GuardReasonCode.MAILBOX_DISABLED,
    GuardReasonCode.MAILBOX_RESTRICTED,
})

# ═══════════════════════════════════════════════════════════════════════════
# Eligible-lead selection query
# ═══════════════════════════════════════════════════════════════════════════


def _eligible_leads_query(
    limit: int,
    min_email_quality: bool = True,
    country_filter: Optional[str] = None,
    country_group: Optional[str] = None,
    industry_group: Optional[str] = None,
    include_low_relevance: bool = False,
    lead_ids: Optional[list] = None,
    status: str = "new",
    source: Optional[str] = None,
    exclude_sources: Optional[list] = None,
    mx_overscan: int = 1,
) -> tuple[str, list]:
    """Build the eligible-lead SELECT for batch sending. Returns (query, params).

    exclude_sources: sources to skip (e.g. the daily NetEase batch must NOT
    consume the 孚盟 customs-data pools, which are reserved for the Zoho
    mailbox). Takes effect only when `source` is not given.

    mx_overscan: fetch `limit × mx_overscan` candidates instead of exactly
    `limit`, in the same priority order, so the caller can MX pre-filter the
    wider pool and still fill `limit` sendable emails. Default 1 = unchanged.
    """
    from emailer.segmentation import SegmentMatcher

    matcher = SegmentMatcher()
    query = "SELECT * FROM leads WHERE status = ?"
    params: list = [status]

    if source:
        query += " AND source = ?"
        params.append(source)
    elif exclude_sources:
        placeholders = ",".join("?" * len(exclude_sources))
        query += f" AND source NOT IN ({placeholders})"
        params.extend(exclude_sources)

    if min_email_quality:
        query += " AND email IS NOT NULL AND email != ''"

    if country_filter:
        query += " AND country LIKE ?"
        params.append(f"%{country_filter}%")

    if country_group:
        countries = matcher.countries_for_group(country_group)
        if countries:
            placeholders = ",".join("?" * len(countries))
            query += f" AND country IN ({placeholders})"
            params.extend(countries)
        else:
            others: list[str] = []
            for g in matcher.country_group_names():
                if g != "generic":
                    others.extend(matcher.countries_for_group(g))
            if others:
                placeholders = ",".join("?" * len(others))
                query += f" AND country NOT IN ({placeholders})"
                params.extend(others)

    if industry_group:
        pred = matcher.group_predicate(industry_group)
        if pred:
            query += f" AND {pred}"

    if not include_low_relevance:
        low_pred = matcher.low_relevance_predicate()
        if low_pred:
            query += f" AND NOT {low_pred}"

    if lead_ids:
        placeholders = ",".join("?" * len(lead_ids))
        query += f" AND id IN ({placeholders})"
        params.extend(lead_ids)

    # Exclude any lead that has ever received an outbound email (permanent
    # safety net — primary guard is status='new').
    # NOTE: must use NOT EXISTS, not `id NOT IN (SELECT lead_id …)`: the
    # subquery contains rows with lead_id IS NULL (legacy 2026-05 sends), and
    # SQL `NOT IN` with a NULL in the list evaluates to NULL for every row →
    # 0 eligible leads → batch silently sends nothing (regression since 08-02).
    query += """ AND NOT EXISTS (
        SELECT 1 FROM communications c
        WHERE c.lead_id = leads.id
          AND c.channel = 'email' AND c.direction = 'outbound'
    )"""

    query += f" ORDER BY {matcher.priority_case()}, discovered_at DESC LIMIT ?"
    params.append(limit * max(1, mx_overscan))
    return query, params


# ═══════════════════════════════════════════════════════════════════════════
# In-memory batch status — registry keyed by SMTP profile so two batches
# (e.g. the NetEase 200/day batch and the Zoho 20/day 孚盟 batch) can run
# concurrently without clobbering each other's progress.
# ═══════════════════════════════════════════════════════════════════════════

# Empty template every per-profile entry starts from a copy of.
_EMPTY_STATUS: dict = {
    "running": False,
    "total": 0,
    "sent": 0,
    "errors": 0,
    "skipped": 0,
    "current_company": "",
    "current_email": "",
    "subject": "",
    "started_at": None,
    "last_update": None,
    "category": "",
    "mailbox": "default",
}

# profile ("default" / "zoho") → status dict. One writer thread per profile;
# the GIL makes each per-dict write atomic, so profiles never interleave.
_batch_status: dict[str, dict] = {}


def _status_key(smtp_profile: str) -> str:
    return smtp_profile or "default"


def get_batch_status(profile: Optional[str] = None) -> dict:
    """Snapshot of batch progress.

    With profile → that mailbox's status alone (e.g. get_batch_status("zoho")).
    Without → aggregate across all profiles: `running` is true if any batch is
    running, counts are summed, and a `profiles` map exposes each mailbox.
    """
    if profile:
        return dict(_batch_status.get(_status_key(profile), dict(_EMPTY_STATUS)))
    if not _batch_status:
        return dict(_EMPTY_STATUS)

    agg = dict(_EMPTY_STATUS)
    agg["running"] = any(s.get("running") for s in _batch_status.values())
    agg["total"] = sum(s.get("total", 0) for s in _batch_status.values())
    agg["sent"] = sum(s.get("sent", 0) for s in _batch_status.values())
    agg["errors"] = sum(s.get("errors", 0) for s in _batch_status.values())
    agg["skipped"] = sum(s.get("skipped", 0) for s in _batch_status.values())

    # Surface the most recent active run's "currently sending to" fields.
    active = [s for s in _batch_status.values() if s.get("running")]
    if active:
        current = max(active, key=lambda s: s.get("started_at") or "")
        agg["current_company"] = current.get("current_company", "")
        agg["current_email"] = current.get("current_email", "")
        agg["subject"] = current.get("subject", "")
    started = [s for s in _batch_status.values() if s.get("started_at")]
    if started:
        latest = max(started, key=lambda s: s.get("started_at") or "")
        agg["started_at"] = latest.get("started_at")
        agg["last_update"] = latest.get("last_update")
        agg["category"] = latest.get("category")
    agg["profiles"] = {k: dict(v) for k, v in _batch_status.items()}
    return agg


def segment_counts(db_path=None, status: str = "new") -> dict:
    """Count leads per industry/country segment (for UI overview charts)."""
    from pathlib import Path

    from emailer.segmentation import SegmentMatcher

    matcher = SegmentMatcher()
    target = Path(db_path) if db_path else config.DB_PATH
    if not target.exists():
        return {"industry_groups": {}, "country_groups": {}, "low_relevance": 0}

    conn = sqlite3.connect(str(target))
    try:
        industry: dict[str, int] = {}
        low = 0
        rows = conn.execute(
            "SELECT industry, COUNT(*) FROM leads WHERE status = ? "
            "GROUP BY industry",
            (status,),
        ).fetchall()
        for label, n in rows:
            group, is_low = matcher.industry_group(label)
            industry[group] = industry.get(group, 0) + n
            if is_low:
                low += n

        countries: dict[str, int] = {}
        for cname, n in conn.execute(
            "SELECT country, COUNT(*) FROM leads WHERE status = ? "
            "GROUP BY country",
            (status,),
        ).fetchall():
            group = matcher.country_group(cname)
            countries[group] = countries.get(group, 0) + n

        return {
            "industry_groups": industry,
            "country_groups": countries,
            "low_relevance": low,
        }
    finally:
        conn.close()


# TODO(future): batch-send logic is duplicated 3× — this module (canonical),
# app.py email_send_batch (API), ui/email_send.py _execute_batch_send (UI).
# Consolidate into a single core function before adding features here.


def _mx_prefilter(guard: "AntiSpamGuard", candidates, limit: int) -> tuple[list, int]:
    """Keep the first `limit` candidates whose email domain passes the MX check.

    Called after `_eligible_leads_query` has fetched a wider pool (`limit ×
    overscan`) in priority order, so pre-filtering preserves prioritization:
    the highest-priority leads that can actually receive mail are the ones sent.
    DNS errors fail open (a lead is kept) to avoid blocking valid emails during
    transient outages — same policy as `can_send`.

    Returns:
        (kept, dropped) — kept rows (≤ limit) and count of no-MX rows skipped.
    """
    kept: list = []
    dropped = 0
    for row in candidates:
        email = (row["email"] or "").strip()
        if email and guard._check_mx(email):
            kept.append(row)
            if len(kept) >= limit:
                break
        else:
            dropped += 1
    return kept, dropped


def send_batch(
    limit: int = 20,
    dry_run: bool = False,
    country_filter: Optional[str] = None,
    country_group: Optional[str] = None,
    industry_group: Optional[str] = None,
    include_low_relevance: bool = False,
    category: Optional[str] = None,
    template_id: Optional[str] = None,
    min_email_quality: bool = True,
    lead_ids: Optional[list] = None,
    status: str = "new",
    source: Optional[str] = None,
    exclude_sources: Optional[list] = None,
    smtp_profile: str = "default",
    mx_prefilter: bool = True,
) -> dict:
    """Send a batch of emails to leads with template rotation.

    SYNCHRONOUS (stdlib smtplib via send_email_sync). SMTP sending is
    sequential (one message + inter-send delay at a time), so asyncio buys
    nothing here — and the async version repeatedly had its event-loop
    executor torn down when run concurrently with the asyncio crawls in the
    same process on Windows (observed 2026-08-05).

    Args:
        limit: Max number of emails to send.
        dry_run: If True, preview without actually sending.
        country_filter: Optional country to target (e.g. "Germany").
        country_group: Optional country-group segment (e.g. "europe").
        industry_group: Optional industry-group segment (e.g. "automotive").
        include_low_relevance: If True, include low-relevance industries.
        category: Optional legacy category ("general"/"custom"); when set,
            uses old category rotation instead of per-lead matching.
        template_id: Optional exact template id. When set, every message in
            this batch uses that template; this is used by the confirmed Web
            Email Center preview so execution matches what the operator saw.
        min_email_quality: If True, skip leads with no email.
        lead_ids: Optional explicit lead ids to target (API/UI).
        status: Lead status filter (default "new").
        source: Optional lead source filter (e.g. "孚盟数据").
        exclude_sources: Sources to skip when `source` is not set — used by
            the NetEase batch so it never consumes the 孚盟 customs-data pools
            reserved for the Zoho mailbox.
        smtp_profile: SMTP profile to send through — "default" (NetEase) or
            "zoho" (personal Zoho mailbox, FTA_ZOHO_SMTP_*). Also the key
            under which this batch's live progress is reported.
        mx_prefilter: If True (default), fetch a wider candidate pool
            (`limit × config.MX_OVERSCAN_FACTOR`) and keep only the top
            `limit` leads whose email domain has an MX record. The send loop
            then hits near-zero MX blocks instead of wasting the batch on
            ~85% no-MX leads. Disable to keep exact `limit` (e.g. when
            `lead_ids` are explicit picks the caller already vetted).

    Returns:
        {"sent": int, "skipped": int, "errors": int, "details": [...]}
    """
    from emailer.templates import TemplateLoader
    from emailer.rotation import AntiSpamGuard, compute_send_delay
    from emailer.send_core import (
        SendIntent,
        deliver,
        evaluate_send,
        global_dry_run_enabled,
        simulate_delivery,
    )

    # ── Init batch status (per-profile — see get_batch_status) ──────────
    # NOTE: the registry entry is `prog` so the `status: str` parameter below
    # (lead-status filter) stays visible for _eligible_leads_query.
    prog = _batch_status.setdefault(_status_key(smtp_profile), dict(_EMPTY_STATUS))
    prog.update(
        running=True, total=0, sent=0, errors=0, skipped=0,
        current_company="", current_email="", subject="",
        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        last_update=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        category=category,
        mailbox=smtp_profile,
    )

    # ── Load templates ──────────────────────────────────────────────────
    loader = TemplateLoader()
    if loader.template_count == 0:
        logger.error("No templates loaded. Create templates/email/templates.json")
        prog["running"] = False
        return {"sent": 0, "skipped": 0, "errors": 0, "details": [],
                "fatal": "No templates available"}

    logger.info(f"Loaded {loader.template_count} templates in categories: {loader.categories}")
    logger.info(f"Using category: '{category}'")
    fixed_template = loader.get_by_id(template_id) if template_id else None
    if template_id and fixed_template is None:
        prog["running"] = False
        return {"sent": 0, "skipped": 0, "errors": 0, "details": [],
                "fatal": f"Template not found: {template_id}"}

    # ── Get leads ────────────────────────────────────────────────────────
    db_path = config.DB_PATH
    if not db_path.exists():
        prog["running"] = False
        return {"sent": 0, "skipped": 0, "errors": 0, "details": [],
                "fatal": "Database not found — run collection first"}

    # busy_timeout: two concurrent batches (NetEase + Zoho) write to the same
    # SQLite file; WAL + a generous lock wait absorbs their occasional overlap
    # instead of raising "database is locked" mid-send.
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    try:
        # Build query (NULL-safe outbound exclusion — see _eligible_leads_query).
        # When MX pre-filtering is on, fetch a wider candidate pool so the
        # pre-filter can still fill `limit` sendable emails (~85% of collected
        # leads have no MX record — see config.MX_OVERSCAN_FACTOR).
        overscan = config.MX_OVERSCAN_FACTOR if mx_prefilter else 1
        # Archived sources stay in CRM for audit/review, but should not flow
        # into an ordinary batch accidentally.  An explicit ``source=`` is
        # still authoritative for an operator-led review or the Zoho Fumeng
        # pool, matching _eligible_leads_query's existing precedence rule.
        effective_exclusions = list(config.OUTREACH_ARCHIVED_SOURCES)
        if exclude_sources:
            effective_exclusions.extend(exclude_sources)
        effective_exclusions = list(dict.fromkeys(effective_exclusions))

        query, params = _eligible_leads_query(
            limit=limit,
            min_email_quality=min_email_quality,
            country_filter=country_filter,
            country_group=country_group,
            industry_group=industry_group,
            include_low_relevance=include_low_relevance,
            lead_ids=lead_ids,
            status=status,
            source=source,
            exclude_sources=effective_exclusions,
            mx_overscan=overscan,
        )

        candidates = conn.execute(query, params).fetchall()

        if not candidates:
            prog["running"] = False
            return {"sent": 0, "skipped": 0, "errors": 0, "details": [],
                    "message": "No eligible leads found"}

        # ── Anti-spam guard (per-mailbox: Zoho's 20/day quota is separate) ─
        guard = AntiSpamGuard(conn, mailbox=smtp_profile)

        # ── P4 SendingGuard: the single pre-send decision engine. It reuses
        # this guard's quota/MX/domain state (pre_send_verdict), the canonical
        # suppression policy, and the mailbox-health seam on the same
        # connection. Every real send below must pass it before the transport.
        sending_guard = build_sending_guard(
            conn, mailbox=smtp_profile, anti_spam_guard=guard
        )

        # ── MX pre-filter: narrow the candidate pool to sendable emails ──
        if mx_prefilter:
            leads, dropped = _mx_prefilter(guard, candidates, limit)
            logger.info(
                f"MX pre-filter: {len(candidates)} candidates → "
                f"{len(leads)} valid (dropped {dropped} no-MX)"
            )
            if len(leads) < limit:
                logger.warning(
                    f"MX pre-filter: only {len(leads)}/{limit} leads passed MX — "
                    "batch will be short"
                )
        else:
            leads = candidates

        logger.info(
            f"Selected {len(leads)} leads for batch send"
            + (f" (country={country_filter})" if country_filter else "")
        )

        prog["total"] = len(leads)
        prog["last_update"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if not leads:
            prog["running"] = False
            return {"sent": 0, "skipped": 0, "errors": 0, "details": [],
                    "message": "No eligible leads with a valid MX domain"}

        results = {"sent": 0, "skipped": 0, "errors": 0, "details": []}

        for i, lead in enumerate(leads):
            lead_dict = dict(lead)

            company_name = lead_dict.get("company_name") or "your company"
            contact_name = lead_dict.get("contact_name")
            email = (lead_dict.get("email") or "").strip()
            country = lead_dict.get("country") or ""
            industry = lead_dict.get("industry") or "industrial supply"
            source = lead_dict.get("source") or ""

            if not email:
                results["skipped"] += 1
                results["details"].append({
                    "company": company_name, "email": "",
                    "status": "skipped", "reason": "no email",
                })
                continue

            # ── SendingGuard decision (evaluate + record; no SMTP) ────
            intent = SendIntent(
                email=email,
                to_name=contact_name,
                lead_id=lead_dict["id"],
                source=source,
                smtp_profile=smtp_profile,
            )
            evaluated = evaluate_send(intent, sending_guard=sending_guard)
            decision = evaluated.decision
            if decision.action is not GuardAction.ALLOW:
                reason = decision.message
                logger.warning(
                    "  BLOCKED: %s — %s [%s]",
                    email, reason, decision.reason_code.value,
                )
                results["skipped"] += 1
                results["details"].append({
                    "company": company_name, "email": email,
                    "status": "skipped", "reason": reason,
                })
                if decision.reason_code in _BATCH_STOP_REASON_CODES:
                    logger.info("Daily cap / mailbox hold reached — stopping batch")
                    break
                continue

            # ── Pick template (rotation) ──────────────────────────────
            if fixed_template is not None:
                template = fixed_template
            elif category:
                template = loader.pick(category)
            else:
                template = loader.pick_for_lead(lead_dict)
            if template is None:
                results["errors"] += 1
                results["details"].append({
                    "company": company_name, "email": email,
                    "status": "error", "reason": "no template",
                })
                continue

            # ── Render email ─────────────────────────────────────────
            rendered = loader.render_email(
                template,
                company_name=company_name,
                contact_name=contact_name,
                country=country,
                industry=industry,
                source=source,
            )

            if dry_run:
                logger.info(
                    f"  [DRY RUN] #{i+1}/{len(leads)}: "
                    f"\"{rendered['subject']}\" → {email} "
                    f"({template['id']})"
                )
                results["details"].append({
                    "company": company_name,
                    "email": email,
                    "status": "dry_run",
                    "subject": rendered["subject"],
                    "template_id": template["id"],
                    "template_name": template["name"],
                })
                # Preserve the existing explicit CLI/UI preview contract.
                # Template rotation is in-memory only in dry-run mode.
                loader.mark_used(template["id"])
                results["sent"] += 1
                continue

            if global_dry_run_enabled():
                # Reuse the canonical P10 outcome/log seam before any claim.
                # That makes this simulation as auditable as every single-send
                # entry while leaving the lead, quota and communications intact.
                outcome = simulate_delivery(
                    evaluated.with_rendered_content(
                        subject=rendered["subject"],
                        body_html=rendered["body_html"],
                        template_id=template["id"],
                    )
                )
                results["skipped"] += 1
                results["details"].append({
                    "company": company_name,
                    "email": email,
                    "status": "global_dry_run",
                    "subject": rendered["subject"],
                    "template_id": template["id"],
                    "template_name": template["name"],
                })
                continue

            # ── Atomic per-lead claim（TOCTOU 修复，08-17 双批次重复发信）─
            # send_batch 先快照一批 leads（LIMIT + 无 NOT EXISTS 补集），两个
            # 并发批次（如采集 finally 链 + cron 双触发）可能各自持有同一批
            # 快照；若都等发完才 mark_contacted，第二批次无法排除第一批次还
            # 没发完的剩余 → 同一线索被发两次。这里在 SMTP 发送前用一条原子
            # UPDATE 抢占该 lead：
            #   rowcount == 0 → 另一批次已抢走（或已 contacted）→ 跳过
            # 所有 send_batch 调用方都按 status='new' 选人（跟进引擎走
            # send_core 的独立路径，同样过 SendingGuard，但不经过本 claim）。
            claim_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            claimed = conn.execute(
                """UPDATE leads SET status = 'contacted',
                   first_contacted_at = COALESCE(first_contacted_at, ?),
                   last_contacted_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'new'""",
                (claim_now, claim_now, claim_now, lead_dict["id"]),
            )
            conn.commit()
            if claimed.rowcount == 0:
                logger.info(f"  SKIP (already claimed): {email}")
                results["skipped"] += 1
                results["details"].append({
                    "company": company_name, "email": email,
                    "status": "skipped", "reason": "already claimed/contacted",
                })
                continue

            # ── Send ─────────────────────────────────────────────────
            logger.info(
                f"  Sending #{i+1}/{len(leads)}: "
                f"\"{rendered['subject']}\" → {email} "
                f"({template['id']})"
            )

            prog["current_company"] = company_name
            prog["current_email"] = email
            prog["subject"] = rendered["subject"]
            prog["last_update"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            # ── Send through the canonical transport seam ─────────────
            # deliver() only reaches SMTP on the ALLOW decision we already
            # recorded above. A non-ALLOW here (defensive) is a skip, never an
            # SMTP failure.
            outcome = deliver(
                evaluated.with_rendered_content(
                    subject=rendered["subject"],
                    body_html=rendered["body_html"],
                    template_id=template["id"],
                ),
            )
            if outcome.dry_run:
                # P12's authorization gate can fail closed after this legacy
                # batch has claimed the lead.  Undo only our own claim and do
                # not turn a policy refusal into an SMTP failure, quota write,
                # template rotation update, or communication record.
                conn.execute(
                    """UPDATE leads SET status = 'new',
                       first_contacted_at = NULL, last_contacted_at = NULL,
                       updated_at = ? WHERE id = ? AND status = 'contacted'""",
                    (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), lead_dict["id"]),
                )
                conn.commit()
                results["skipped"] += 1
                results["details"].append({
                    "company": company_name, "email": email,
                    "status": "authorization_dry_run", "reason": outcome.message,
                })
                continue
            if outcome.blocked:
                results["skipped"] += 1
                results["details"].append({
                    "company": company_name, "email": email,
                    "status": "skipped", "reason": outcome.message,
                })
                continue
            success, msg, msg_id = outcome.success, outcome.message, outcome.message_id

            if success:
                # Record success. _record_quota_safe absorbs a transient
                # "database is locked" from a concurrent batch's write instead
                # of letting it abort the loop after the email already went out.
                _record_quota_safe(guard, email)
                loader.mark_used(template["id"])
                _log_communication(conn, lead_dict["id"], rendered, msg_id)
                results["sent"] += 1
                prog["sent"] += 1
                results["details"].append({
                    "company": company_name, "email": email,
                    "status": "sent",
                    "template_id": template["id"],
                })
            else:
                is_hard_bounce = guard.record_smtp_failure(
                    email,
                    msg,
                    provider=smtp_profile,
                )
                msg = sanitize_diagnostic(msg)
                logger.warning("  DELIVERY FAILED: %s", msg)
                if is_hard_bounce:
                    # Mark lead as dead so it won't be retried
                    try:
                        conn.execute(
                            "UPDATE leads SET status = 'dead', updated_at = ? WHERE id = ?",
                            (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), lead_dict["id"]),
                        )
                        conn.commit()
                    except Exception:
                        pass
                else:
                    # 非硬退信失败（临时错误）→ 回滚上面的原子 claim，把线索
                    # 放回 'new' 并清掉 claim 误写的联系时间戳，让后续批次可重试。
                    # 仅在 status='contacted'（即本 claim 抢到的那条）时回滚，
                    # 绝不覆盖并发批次已改的状态。
                    try:
                        conn.execute(
                            """UPDATE leads SET status = 'new',
                               first_contacted_at = NULL, last_contacted_at = NULL,
                               updated_at = ? WHERE id = ? AND status = 'contacted'""",
                            (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), lead_dict["id"]),
                        )
                        conn.commit()
                    except Exception:
                        pass
                results["errors"] += 1
                prog["errors"] += 1
                results["details"].append({
                    "company": company_name, "email": email,
                    "status": "error", "reason": msg,
                })

            # ── Inter-send delay ─────────────────────────────────────
            if i < len(leads) - 1:
                delay = compute_send_delay()
                logger.debug(f"  Waiting {delay:.0f}s before next send...")
                time.sleep(delay)

        prog["running"] = False
        prog["last_update"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return results

    finally:
        prog["running"] = False
        prog["last_update"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.close()


def _log_communication(conn: sqlite3.Connection, lead_id: int, rendered: dict, message_id: Optional[str] = None):
    """Record a sent email in the communications table and update lead status.

    If the manager-layer write fails (e.g. constraint violation), falls back to
    raw SQL to guarantee the lead is marked contacted. The email was already sent
    over SMTP at this point — we must not leave the lead in 'new' status.
    """
    import sqlite3 as _sqlite3
    try:
        from crm.manager import CommunicationManager, LeadManager
        from crm.models import Communication

        comm_mgr = CommunicationManager(conn)
        lead_mgr = LeadManager(conn)

        comm = Communication(
            lead_id=lead_id,
            channel="email",
            direction="outbound",
            subject=rendered["subject"],
            body=rendered["body_html"],
            status="sent",
            template_id=rendered.get("template_id"),
            message_id=message_id,
        )
        comm_mgr.log(comm)
        lead_mgr.mark_contacted(lead_id)
    except _sqlite3.Error as e:
        logger.error(f"DB error logging communication for lead {lead_id}: {e}")
        # Fallback: at minimum, mark lead as contacted via raw SQL so it
        # cannot be re-picked. The full communication record can be
        # reconstructed from the batch log if needed.
        try:
            now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT OR IGNORE INTO communications
                   (lead_id, channel, direction, subject, status, sent_at)
                   VALUES (?, 'email', 'outbound', ?, 'sent', ?)""",
                (lead_id, rendered.get("subject", "unknown"), now_utc),
            )
            conn.execute(
                """UPDATE leads SET status = 'contacted',
                   first_contacted_at = COALESCE(first_contacted_at, ?),
                   last_contacted_at = ?, updated_at = ?
                   WHERE id = ?""",
                (now_utc, now_utc, now_utc, lead_id),
            )
            conn.commit()
            logger.warning(f"Fallback: marked lead {lead_id} as contacted via raw SQL")
        except Exception:
            logger.exception(
                f"CRITICAL: Could not log communication or update status "
                f"for lead {lead_id}. Email was SENT but DB not updated. "
                f"Manual fix required."
            )
    except Exception as e:
        logger.exception(f"Unexpected error logging communication for lead {lead_id}: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Concurrency-safe quota recording + Zoho 孚盟 daily driver
# ═══════════════════════════════════════════════════════════════════════════


def _record_quota_safe(guard, email: str) -> None:
    """Increment a mailbox's send quota, retrying transient SQLite locks.

    Called AFTER the SMTP send succeeded. If two batches (NetEase + Zoho)
    commit to the same DB at the same moment, one may briefly hit
    "database is locked"; the email is already out, so that must not abort
    the whole batch. Retry a couple times, then log and continue — a quota
    counter being off by one is the lesser evil vs killing the send loop.
    """
    import time as _time

    for attempt in range(3):
        try:
            guard.record_send(email)
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                _time.sleep(1 + attempt)
                continue
            logger.error("Failed to record send quota for %s: %s", email, e)
            return
        except Exception as e:
            logger.error("Failed to record send quota for %s: %s", email, e)
            return


# The 孚盟 customs-data pools both go through the personal Zoho mailbox.
# 孚盟数据 (high quality) drains first; 孚盟数据普通 follows when quota allows.
ZOHO_FUMENG_SOURCES = ("孚盟数据", "孚盟数据普通")


def run_zoho_daily_batch(dry_run: bool = False) -> dict:
    """Send the 孚盟 customs-data pools through the personal Zoho mailbox.

    Drains 孚盟数据 (high quality) first, then 孚盟数据普通. The shared Zoho
    20/day quota (mailbox_quotas) stops the pair at FTA_ZOHO_DAILY_LIMIT total,
    so calling daily is safe no matter how much the manual CLI already sent.

    Meant to run on its own scheduler thread — fully independent of the NetEase
    200/day batch (separate quota row, separate SMTP profile), so the two
    channels never block each other.

    Returns:
        {"sent", "skipped", "errors", "details", "pools", "fatal"?}
    """
    db_path = config.DB_PATH
    if not db_path.exists():
        return {"sent": 0, "skipped": 0, "errors": 0, "details": [],
                "pools": [], "fatal": "Database not found — run collection first"}

    results = {"sent": 0, "skipped": 0, "errors": 0, "details": [], "pools": []}

    # Peek at today's remaining Zoho quota to size the pools. send_batch's own
    # AntiSpamGuard enforces the real cap regardless — this is just a starting
    # budget so 孚盟数据普通 only runs when there's room after the high-quality pool.
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        from emailer.rotation import AntiSpamGuard
        guard = AntiSpamGuard(conn, mailbox="zoho")
        remaining = guard.get_status()["remaining"]
    finally:
        conn.close()

    for source in ZOHO_FUMENG_SOURCES:
        if remaining <= 0:
            logger.info("Zoho quota exhausted — remaining pool %s skipped", source)
            break
        res = send_batch(
            limit=remaining,
            dry_run=dry_run,
            source=source,
            smtp_profile="zoho",
        )
        sent = res.get("sent", 0)
        results["pools"].append({
            "source": source,
            "sent": sent,
            "skipped": res.get("skipped", 0),
            "errors": res.get("errors", 0),
        })
        results["sent"] += sent
        results["skipped"] += res.get("skipped", 0)
        results["errors"] += res.get("errors", 0)
        results["details"].extend(res.get("details", []))
        if res.get("fatal"):
            results["fatal"] = res["fatal"]
            break
        remaining -= sent
    return results


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


def main():
    """CLI entry point for emailer module."""
    import argparse

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="python -m emailer",
        description="FTA Email Sender — 批量邮件发送",
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # ── send ────────────────────────────────────────────────────────────
    send_parser = subparsers.add_parser("send", help="Send batch emails")
    send_parser.add_argument(
        "--category", "-c",
        default=None,
        choices=["general", "custom"],
        help="Legacy template category (general/custom). Omit to auto-match by industry.",
    )
    send_parser.add_argument(
        "--limit", "-l",
        type=int, default=20,
        help="Max emails to send (default: 20)",
    )
    send_parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Preview without sending",
    )
    send_parser.add_argument(
        "--country", "-C",
        help="Target specific country (e.g. Germany)",
    )
    send_parser.add_argument(
        "--country-group",
        help="Target a country group (north_america, europe, northeast_asia, oceania, southeast_asia, middle_east, generic)",
    )
    send_parser.add_argument(
        "--industry-group",
        help="Target an industry group (automotive, machinery, construction, electronics, new_energy, medical, hardware_furniture, general)",
    )
    send_parser.add_argument(
        "--include-low-relevance",
        action="store_true",
        help="Include low-relevance industries (Food, Footwear, Toys, ...) in the batch",
    )
    send_parser.add_argument(
        "--source",
        help="Only send to leads from this source (e.g. 孚盟数据)",
    )
    send_parser.add_argument(
        "--smtp",
        choices=["default", "zoho"],
        default="default",
        help="SMTP profile: default=NetEase (FTA_SMTP_*), zoho=personal Zoho mailbox",
    )

    # ── send-zoho ────────────────────────────────────────────────────────
    send_zoho_parser = subparsers.add_parser(
        "send-zoho",
        help="Send 孚盟 customs-data pools (孚盟数据 then 孚盟数据普通) via Zoho",
    )
    send_zoho_parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Preview without actually sending",
    )

    # ── preview ─────────────────────────────────────────────────────────
    subparsers.add_parser("preview", help="Preview all email templates")

    # ── status ──────────────────────────────────────────────────────────
    status_parser = subparsers.add_parser("status", help="Show anti-spam quota status")
    status_parser.add_argument(
        "--smtp",
        choices=["default", "zoho"],
        default="default",
        help="Quota to show: default=NetEase batch, zoho=personal Zoho mailbox",
    )

    # ── sync-templates ──────────────────────────────────────────────────
    sync_parser = subparsers.add_parser(
        "sync-templates",
        help="Sync templates/email/templates.json into the DB email_templates table",
    )
    sync_parser.add_argument(
        "--db",
        help="Database path (default: config.DB_PATH)",
    )

    # ── segments ────────────────────────────────────────────────────────
    subparsers.add_parser(
        "segments",
        help="Show lead counts per country/industry segment",
    )

    args = parser.parse_args()

    if args.command == "preview":
        from emailer.templates import TemplateLoader
        loader = TemplateLoader()
        print(f"\n{'='*60}")
        print(f"Email Templates ({loader.template_count} total)")
        print(f"{'='*60}")
        for p in loader.preview_all():
            print(f"\n--- [{p['category']}] {p['name']} ({p['id']}) ---")
            print(f"Subject: {p['subject']}")
            print(f"Body preview: {p['body_preview']}")
            print(f"Usage: {p['usage_count']} | Last: {p['last_used'] or 'never'}")
        return

    if args.command == "status":
        db_path = config.DB_PATH
        if not db_path.exists():
            print("Database not found.")
            return
        conn = sqlite3.connect(str(db_path))
        try:
            from emailer.rotation import AntiSpamGuard
            guard = AntiSpamGuard(conn, mailbox=args.smtp)
            status = guard.get_status()
            print(f"\n{'='*40}")
            print(f"Anti-Spam Quota Status [{args.smtp}]")
            print(f"{'='*40}")
            print(f"Sent today:       {status['emails_sent_today']}/{status['daily_limit']}")
            print(f"Remaining:        {status['remaining']}")
            print(f"Per-domain limit: {status['per_domain_limit']}")
            print(f"Domain counts:    {status['domain_counts']}")
            print(f"Next reset:       {status['next_reset']}")
        finally:
            conn.close()
        return

    if args.command == "sync-templates":
        from pathlib import Path
        from emailer.templates import sync_templates_to_db

        db_path = Path(args.db) if args.db else config.DB_PATH
        result = sync_templates_to_db(db_path)
        print(f"\nSynced {result['total']} templates "
              f"({result['inserted']} inserted, {result['updated']} updated, "
              f"{result['deactivated']} deactivated)")
        return

    if args.command == "segments":
        counts = segment_counts()
        print(f"\nIndustry segments (new leads):")
        for group, n in sorted(
            counts["industry_groups"].items(),
            key=lambda kv: -kv[1],
        ):
            print(f"  {group:<22} {n}")
        print(f"  {'low_relevance':<22} {counts['low_relevance']}")
        print(f"\nCountry segments (new leads):")
        for group, n in sorted(
            counts["country_groups"].items(),
            key=lambda kv: -kv[1],
        ):
            print(f"  {group:<22} {n}")
        return

    if args.command == "send":
        results = send_batch(
            category=args.category,
            limit=args.limit,
            dry_run=args.dry_run,
            country_filter=args.country,
            country_group=args.country_group,
            industry_group=args.industry_group,
            include_low_relevance=args.include_low_relevance,
            source=args.source,
            smtp_profile=args.smtp,
        )

        if results.get("fatal"):
            print(f"\nERROR: {results['fatal']}")
            return

        # Summary
        print(f"\n{'='*60}")
        print(f"Batch Send Results")
        print(f"{'='*60}")
        print(f"Sent:       {results['sent']}")
        print(f"Skipped:    {results['skipped']}")
        print(f"Errors:     {results['errors']}")
        print(f"{'='*60}")

        if args.dry_run:
            print("\n[Dry Run Preview]")
            for d in results.get("details", []):
                print(f"  → {d['company']} | {d['email']} | {d.get('subject', '')[:60]}")
        else:
            sent_details = [d for d in results.get("details", []) if d["status"] == "sent"]
            error_details = [d for d in results.get("details", []) if d["status"] == "error"]
            if sent_details:
                print(f"\nSent to:")
                for d in sent_details:
                    print(f"  OK  {d['company']} ({d['email']})")
            if error_details:
                print(f"\nErrors:")
                for d in error_details:
                    print(f"  ERR {d['company']} ({d['email']}) — {d.get('reason', 'unknown')}")
        return

    if args.command == "send-zoho":
        results = run_zoho_daily_batch(dry_run=args.dry_run)
        if results.get("fatal"):
            print(f"\nERROR: {results['fatal']}")
            return
        print(f"\n{'='*60}")
        print("Zoho 孚盟 Daily Batch")
        print(f"{'='*60}")
        for p in results["pools"]:
            print(
                f"  {p['source']}: sent={p['sent']}, "
                f"skipped={p['skipped']}, errors={p['errors']}"
            )
        print(f"  TOTAL: sent={results['sent']}, "
              f"skipped={results['skipped']}, errors={results['errors']}")
        if args.dry_run:
            print("\n[Dry Run Preview]")
            for d in results.get("details", []):
                print(f"  → {d['company']} | {d['email']} | {d.get('subject', '')[:60]}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
