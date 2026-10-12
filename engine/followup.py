"""
FollowUpEngine — multi-stage email follow-up automation.

Stages:
  1. Day 3-7:   Gentle Follow-up with Catalog (followup_01)
  2. Day 10-16: Industry Insight Follow-up (followup_02)
  3. Day 25-32: Final Follow-up — Last Contact (followup_03)
  4. Day 35+:   Mark as dead if no reply to any stage

The engine queries communications history to determine each lead's current stage,
picks the correct template, sends via SMTP (with anti-spam enforcement), and records
the outreach in the communications table.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
from emailer.send_core import SendIntent, global_dry_run_enabled, send_one
from services.email_reputation.diagnostic_safety import sanitize_diagnostic

logger = logging.getLogger("engine.followup")

# ═══════════════════════════════════════════════════════════════════════════
# Stage definitions
# ═══════════════════════════════════════════════════════════════════════════

STAGE_DEFINITIONS = {
    1: {
        "template_id": "followup_01",
        "min_days": 3,
        "max_days": 7,
        "prev_template_pattern": "general_%",
        "description": "Gentle follow-up — catalog offer",
    },
    2: {
        "template_id": "followup_02",
        "min_days": 10,
        "max_days": 16,
        "prev_template_pattern": "followup_01",
        "description": "Industry insight — value-add",
    },
    3: {
        "template_id": "followup_03",
        "min_days": 25,
        "max_days": 32,
        "prev_template_pattern": "followup_02",
        "description": "Final follow-up — last contact",
    },
}

DEAD_THRESHOLD_DAYS = 35


@dataclass
class FollowUpResult:
    """Summary of a follow-up run for one stage."""
    stage: int = 0
    candidates_found: int = 0
    sent: int = 0
    skipped: int = 0
    errors: int = 0
    dead_marked: int = 0
    details: list[dict] = field(default_factory=list)
    fatal: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# FollowUpEngine
# ═══════════════════════════════════════════════════════════════════════════


class FollowUpEngine:
    """Multi-stage follow-up automation for cold outreach.

    Usage:
        from engine.followup import FollowUpEngine
        engine = FollowUpEngine(Path("data/fta.db"))
        total = engine.run()   # runs stages 1-3 + marks dead leads
    """

    def __init__(
        self,
        db_path: Path,
        dry_run: bool = False,
        max_per_stage: int = 200,
    ):
        self.db_path = db_path
        self.dry_run = dry_run
        self.max_per_stage = max_per_stage

        self._template_by_id: dict[str, dict] = {}
        self._renderer = None
        self._load_templates()

    def _load_templates(self):
        """Load templates from JSON and index by template ID."""
        from emailer.templates import TemplateLoader
        loader = TemplateLoader()
        self._renderer = loader.renderer
        for t in loader._templates:
            self._template_by_id[t["id"]] = t
        logger.debug(
            "Loaded %d templates: %s",
            len(self._template_by_id),
            list(self._template_by_id.keys()),
        )

    # ── Candidate queries ─────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def get_candidates(self, stage: int) -> list[dict]:
        """Get leads eligible for a specific follow-up stage."""
        if stage not in STAGE_DEFINITIONS:
            logger.error("Invalid follow-up stage: %s", stage)
            return []

        sd = STAGE_DEFINITIONS[stage]
        template_id = sd["template_id"]

        if template_id not in self._template_by_id:
            logger.error(
                "Template '%s' not found — follow-up stage %s cannot run",
                template_id, stage,
            )
            return []

        conn = self._get_conn()
        try:
            query = """
                SELECT l.*, c.id AS matched_comm_id, c.sent_at AS last_outbound_at
                FROM leads l
                JOIN communications c ON c.lead_id = l.id
                WHERE l.status = 'contacted'
                  AND l.email IS NOT NULL AND l.email != ''
                  AND c.direction = 'outbound'
                  AND c.status = 'sent'
                  AND c.template_id LIKE ?
                  AND c.sent_at <= datetime('now', ?)
                  AND c.sent_at >= datetime('now', ?)
                  AND c.replied_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM communications c2
                      WHERE c2.lead_id = l.id
                        AND c2.template_id = ?
                        AND c2.status = 'sent'
                  )
                  AND c.id = (
                      SELECT c3.id FROM communications c3
                      WHERE c3.lead_id = l.id
                        AND c3.direction = 'outbound'
                        AND c3.status = 'sent'
                        AND c3.template_id LIKE ?
                      ORDER BY c3.sent_at DESC
                      LIMIT 1
                  )
                ORDER BY c.sent_at ASC
                LIMIT ?
            """
            rows = conn.execute(
                query,
                (
                    sd["prev_template_pattern"],
                    f"-{sd['min_days']} days",
                    f"-{sd['max_days']} days",
                    template_id,
                    sd["prev_template_pattern"],
                    self.max_per_stage,
                ),
            ).fetchall()

            candidates = [dict(r) for r in rows]
            logger.info(
                "Stage %s (%s): %s candidates (template=%s, %s-%sd window)",
                stage, sd["description"], len(candidates),
                template_id, sd["min_days"], sd["max_days"],
            )
            return candidates
        finally:
            conn.close()

    def get_dead_candidates(self) -> list[dict]:
        """Get leads where last outbound > DEAD_THRESHOLD_DAYS ago, no reply."""
        conn = self._get_conn()
        try:
            query = """
                SELECT l.*, c.sent_at AS last_outbound_at
                FROM leads l
                JOIN communications c ON c.lead_id = l.id
                WHERE l.status = 'contacted'
                  AND c.direction = 'outbound'
                  AND c.status = 'sent'
                  AND c.sent_at <= datetime('now', ?)
                  AND c.replied_at IS NULL
                  AND c.id = (
                      SELECT c2.id FROM communications c2
                      WHERE c2.lead_id = l.id
                        AND c2.direction = 'outbound'
                        AND c2.status = 'sent'
                      ORDER BY c2.sent_at DESC
                      LIMIT 1
                  )
            """
            rows = conn.execute(
                query, (f"-{DEAD_THRESHOLD_DAYS} days",)
            ).fetchall()
            candidates = [dict(r) for r in rows]
            logger.info(
                "Dead candidates: %s leads (>%sd no reply)",
                len(candidates), DEAD_THRESHOLD_DAYS,
            )
            return candidates
        finally:
            conn.close()

    # ── Sending ───────────────────────────────────────────────────────────

    def _send_one(self, lead: dict, stage: int, sending_guard) -> tuple:
        """Send one follow-up email through the canonical guarded seam.

        Returns (success, status, message_id, subject, body_html) — the rendered
        subject/body are threaded back so the DB records what was actually sent
        (previously only the raw template text was stored).

        ``sending_guard`` is the SendingGuard built once per run_stage on a
        persistent connection; every real send must pass it (evaluate + record)
        before the transport is reached, so a BLOCK / DELAY / PAUSE_MAILBOX
        decision never touches SMTP and is reported as ``blocked:...`` (skipped,
        not an SMTP failure). SMTP-level failures are still recorded into
        unified suppression via ``record_smtp_failure``.
        """
        sd = STAGE_DEFINITIONS[stage]
        template = self._template_by_id[sd["template_id"]]

        # ── Render ──────────────────────────────────────────────────────
        try:
            subject = self._renderer.render_subject(
                template["subject"],
                company_name=lead.get("company_name"),
                contact_name=lead.get("contact_name"),
                country=lead.get("country"),
                industry=lead.get("industry"),
                source=lead.get("source"),
            )
            body_html = self._renderer.render_body(
                template["body"],
                company_name=lead.get("company_name"),
                contact_name=lead.get("contact_name"),
                country=lead.get("country"),
                industry=lead.get("industry"),
                source=lead.get("source"),
            )
        except Exception:
            return False, "render_error", None, None, None

        if self.dry_run:
            logger.info(
                "  [DRY RUN] %s → %s (stage %s)",
                sd["template_id"],
                lead.get("company_name") or lead.get("email"),
                stage,
            )
            return True, "dry_run_skipped", f"dry-run-{lead['id']}-s{stage}", subject, body_html

        # ── Canonical guarded send (evaluate + record, then transport) ──
        # send_one() builds the GuardContext from stable identifiers, records
        # the decision, and only reaches the SMTP transport on ALLOW. The
        # default transport resolves emailer.sender.send_email_sync at call
        # time, so existing test monkeypatches keep intercepting it.
        outcome = send_one(
            SendIntent(
                email=lead.get("email", ""),
                subject=subject,
                body_html=body_html,
                to_name=lead.get("contact_name"),
                lead_id=lead["id"],
                stage=stage,
                source=lead.get("source"),
            ),
            sending_guard=sending_guard,
        )
        if outcome.blocked:
            return False, f"blocked:{outcome.message}", None, subject, body_html
        if outcome.dry_run:
            # P10 or P12 authorization simulation is neither a delivery
            # failure nor a sent follow-up: leave the candidate eligible for
            # a later, explicitly enabled run and do not touch state.
            status = (
                "global_dry_run"
                if global_dry_run_enabled()
                else "authorization_dry_run"
            )
            return False, status, None, subject, body_html
        if not outcome.success:
            # SMTP-level failure — record into unified suppression so the
            # recipient is never retried, then surface a sanitized diagnostic.
            from emailer.rotation import AntiSpamGuard
            failure_conn = sqlite3.connect(str(self.db_path))
            try:
                AntiSpamGuard(failure_conn).record_smtp_failure(
                    lead["email"],
                    outcome.message,
                    provider="default",
                )
            finally:
                failure_conn.close()
            msg = sanitize_diagnostic(outcome.message)
            return False, msg, outcome.message_id, subject, body_html
        return True, "sent", outcome.message_id, subject, body_html

    # ── DB recording ─────────────────────────────────────────────────────

    def _record_send(self, lead: dict, stage: int, message_id, subject: str, body: str):
        """Record a follow-up that was actually sent (status='sent').

        Only real sends are written to communications and count toward the daily
        quota. Previously blocked/errored attempts were also recorded here:
          • the candidate query's NOT EXISTS matched ANY record (even 'failed'),
            so a blocked lead was excluded from that stage forever (it never got
            the email but was treated as already-followed-up); and
          • the quota increment ran regardless of status, inflating email_count
            by the number of blocked attempts (observed 2026-08-05: 249 vs 200
            real sends). Blocked attempts now stay unrecorded and are retried
            on the next scheduled run.
        """
        sd = STAGE_DEFINITIONS[stage]
        template = self._template_by_id[sd["template_id"]]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """INSERT INTO communications
                   (lead_id, template_id, channel, direction, subject, body,
                    status, error_message, message_id, sent_at)
                   VALUES (?, ?, 'email', 'outbound', ?, ?, 'sent', NULL, ?, ?)""",
                (
                    lead["id"], template["id"],
                    subject, body, message_id, now,
                ),
            )
            conn.execute(
                "UPDATE leads SET last_contacted_at = ? WHERE id = ?",
                (now, lead["id"]),
            )
            # Daily quota + per-domain counts via the shared guard (authoritative
            # source — keeps email_count consistent with the batch sender).
            from emailer.rotation import AntiSpamGuard
            guard = AntiSpamGuard(conn)
            guard.record_send(lead.get("email", ""))
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Failed to record follow-up for lead %s", lead["id"])
        finally:
            conn.close()

    def _mark_dead(self, lead: dict):
        """Mark a lead as dead after all follow-ups exhausted."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "UPDATE leads SET status = 'dead', updated_at = ? WHERE id = ?",
                (now, lead["id"]),
            )
            conn.commit()
            logger.info(
                "  DEAD: %s (%s) — last outbound %s",
                lead.get("company_name") or lead.get("email"),
                lead.get("email"),
                lead.get("last_outbound_at"),
            )
        except Exception:
            conn.rollback()
            logger.exception("Failed to mark lead %s as dead", lead["id"])
        finally:
            conn.close()

    # ── Main entry points ────────────────────────────────────────────────

    def run_stage(self, stage: int) -> FollowUpResult:
        """Run follow-ups for a single stage, returning results."""
        sd = STAGE_DEFINITIONS[stage]
        result = FollowUpResult(stage=stage)

        candidates = self.get_candidates(stage)
        result.candidates_found = len(candidates)
        if not candidates:
            return result

        # One SendingGuard on a persistent connection for the whole stage —
        # every lead's send must pass it (suppression + mailbox-health seam +
        # daily-cap/MX/domain via the existing guard) before reaching SMTP.
        from emailer.rotation import compute_send_delay
        from services.email_reputation.sending_guard import build_sending_guard
        guard_conn = sqlite3.connect(str(self.db_path))
        try:
            sending_guard = build_sending_guard(guard_conn, mailbox="default")
            for lead in candidates:
                success, status, message_id, subject, body_html = self._send_one(
                    lead, stage, sending_guard
                )

                if status in ("dry_run_skipped", "global_dry_run", "authorization_dry_run"):
                    result.skipped += 1
                elif success:
                    result.sent += 1
                    self._record_send(lead, stage, message_id, subject, body_html)
                elif status.startswith("blocked:"):
                    result.skipped += 1
                    # Not recorded — blocked attempt, lead is retried next run.
                    # (Recording it made the lead skip this stage forever.)
                else:
                    result.errors += 1

                result.details.append({
                    "lead_id": lead["id"],
                    "company": lead.get("company_name", ""),
                    "email": lead.get("email", ""),
                    "stage": stage,
                    "template": sd["template_id"],
                    "success": success,
                    "status": status,
                    "message_id": message_id,
                })

                # Inter-send delay
                if not self.dry_run and status not in (
                    "dry_run_skipped", "global_dry_run", "authorization_dry_run",
                ) and not global_dry_run_enabled() and (
                    result.sent + result.errors
                ) < len(candidates):
                    time.sleep(compute_send_delay())
        finally:
            guard_conn.close()

        logger.info(
            "Stage %s done: %s sent, %s skipped, %s errors",
            stage, result.sent, result.skipped, result.errors,
        )
        return result

    def run(self) -> dict:
        """Run all follow-up stages + mark dead leads. Returns summary dict."""
        total = {
            "stages": [],
            "total_candidates": 0,
            "total_sent": 0,
            "total_skipped": 0,
            "total_errors": 0,
            "total_dead": 0,
            "dry_run": self.dry_run,
        }

        for stage in [1, 2, 3]:
            try:
                r = self.run_stage(stage)
            except Exception:
                logger.exception("Stage %s exception", stage)
                r = FollowUpResult(stage=stage, fatal=f"Stage {stage} exception")

            if r.stage <= 3:
                total["stages"].append({
                    "stage": r.stage,
                    "description": STAGE_DEFINITIONS[r.stage]["description"],
                    "candidates": r.candidates_found,
                    "sent": r.sent,
                    "skipped": r.skipped,
                    "errors": r.errors,
                })
                total["total_candidates"] += r.candidates_found
                total["total_sent"] += r.sent
                total["total_skipped"] += r.skipped
                total["total_errors"] += r.errors

        # ── Mark dead leads ─────────────────────────────────────────────
        try:
            dead = self.get_dead_candidates()
            for lead in dead:
                self._mark_dead(lead)
                total["total_dead"] += 1
        except Exception:
            logger.exception("Dead-lead marking failed")

        return total
