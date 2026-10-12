"""
Email Template Engine — Jinja2 rendering + JSON template loading + rotation.

Two layers:
  1. EmailTemplateRenderer — low-level Jinja2 rendering with variable context.
  2. TemplateLoader — loads templates from JSON file, provides rotation by category.

Variables available in templates:
- {{ company_name }}          — Lead's company name
- {{ contact_name }}          — Lead's contact person name
- {{ country }}               — Lead's country
- {{ industry }}              — Lead's industry
- {{ source }}                — Where the lead came from
- {{ sender_name }}           — Our sender name (from config)
- {{ sender_company }}        — Our company name
- {{ current_date }}          — Today's date
"""

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

from jinja2 import Template, Environment, BaseLoader, TemplateNotFound

import config
from utils.sanitize import sanitize_context

logger = logging.getLogger(__name__)

# Default template file path
DEFAULT_TEMPLATES_PATH = Path(os.getenv(
    "FTA_TEMPLATES_DIR",
    str(config.BASE_DIR / "templates" / "email"),
)) / "templates.json"


class EmailTemplateRenderer:
    """Renders email templates with Jinja2 using standard context variables."""

    def __init__(self, sender_company: str = "Example Operations"):
        self.sender_company = sender_company
        self.env = Environment(loader=BaseLoader(), autoescape=True)
        self._matcher = None

    def _segment_matcher(self):
        """Lazy SegmentMatcher instance loaded from data/segments.json."""
        if self._matcher is None:
            from emailer.segmentation import SegmentMatcher
            self._matcher = SegmentMatcher()
        return self._matcher

    def _region_for(self, country):
        """Return region variables {greeting, signoff, region_name, country_group}."""
        return self._segment_matcher().region(country)

    def render_subject(
        self,
        subject_template: str,
        company_name: Optional[str] = None,
        contact_name: Optional[str] = None,
        country: Optional[str] = None,
        industry: Optional[str] = None,
        source: Optional[str] = None,
        **extra,
    ) -> str:
        """Render a subject line template."""
        ctx = self._build_context(
            company_name=company_name,
            contact_name=contact_name,
            country=country,
            industry=industry,
            source=source,
            **extra,
        )
        try:
            tmpl = self.env.from_string(subject_template)
            return tmpl.render(ctx).strip()
        except Exception as e:
            logger.warning(f"Subject render failed: {e} — returning raw template")
            return subject_template

    def render_body(
        self,
        body_template: str,
        company_name: Optional[str] = None,
        contact_name: Optional[str] = None,
        country: Optional[str] = None,
        industry: Optional[str] = None,
        source: Optional[str] = None,
        **extra,
    ) -> str:
        """Render an HTML body template.

        Templates are authored as plain text with \\n line breaks.
        This method converts them to basic HTML for proper rendering
        in email clients:
          - Double newlines (\\n\\n) → paragraph breaks (</p><p>)
          - Single newlines (\\n) → line breaks (<br>)
          - Output is wrapped in <p> tags
        """
        ctx = self._build_context(
            company_name=company_name,
            contact_name=contact_name,
            country=country,
            industry=industry,
            source=source,
            **extra,
        )
        try:
            tmpl = self.env.from_string(body_template)
            body = tmpl.render(ctx)
            # If the body already contains HTML tags, return as-is
            # (prevents double-escaping for any future HTML templates)
            if '<' in body and '>' in body:
                return body
            # Convert plain-text newlines to HTML structure
            body = body.strip()
            # Paragraph breaks: double newlines → close + open paragraph
            body = body.replace('\n\n', '</p><p>')
            # Single line breaks → <br>
            body = body.replace('\n', '<br>')
            return f'<p>{body}</p>'
        except Exception as e:
            logger.warning(f"Body render failed: {e}")
            return f"<html><body><p>Template render error: {e}</p></body></html>"

    def preview(
        self,
        subject_template: str,
        body_template: str,
        company_name: str = "ACME Corp",
        contact_name: str = "John Smith",
    ) -> dict:
        """Preview a template with sample data. Returns {subject, body}."""
        return {
            "subject": self.render_subject(
                subject_template,
                company_name=company_name,
                contact_name=contact_name,
                country="USA",
                industry="Automotive",
            ),
            "body": self.render_body(
                body_template,
                company_name=company_name,
                contact_name=contact_name,
                country="USA",
                industry="Automotive",
            ),
        }

    def _build_context(
        self,
        company_name: Optional[str] = None,
        contact_name: Optional[str] = None,
        country: Optional[str] = None,
        industry: Optional[str] = None,
        source: Optional[str] = None,
        **extra,
    ) -> dict:
        """Build the standard Jinja2 template context with sanitized fields.

        All user-facing fields are sanitized to remove Chinese text
        before rendering outbound email templates.
        """
        sanitized = sanitize_context(
            company_name=company_name,
            contact_name=contact_name,
            country=country,
            industry=industry,
        )
        ctx = {
            **sanitized,
            "source": source or "",
            "sender_name": config.SMTP_FROM_NAME,
            "sender_company": self.sender_company,
            "sender_whatsapp": config.SENDER_WHATSAPP,
            "sender_phone": config.SENDER_PHONE,
            "sender_email": config.SENDER_EMAIL,
            "sender_address": config.SENDER_ADDRESS,
            "sender_certs": config.SENDER_CERTS,
            "sender_website": config.SENDER_WEBSITE,
            "current_date": date.today().isoformat(),
            # Pre-built plain-text signature block
            "signature": (
                f"{config.SENDER_NAME}\n"
                f"Foreign Trade Department\n"
                f"{self.sender_company}\n"
                f"{config.SENDER_ADDRESS}\n"
                f"WhatsApp: {config.SENDER_WHATSAPP}\n"
                f"Web: {config.SENDER_WEBSITE}"
            ),
        }
        ctx.update(extra)

        # Region + industry segment variables (from data/segments.json).
        # Explicit kwargs (e.g. greeting="...") always win over derived values.
        region = self._region_for(ctx.get("country"))
        if not ctx.get("greeting"):
            ctx["greeting"] = region["greeting"]
        if not ctx.get("signoff"):
            ctx["signoff"] = region["signoff"]
        if not ctx.get("region_name"):
            ctx["region_name"] = region["region_name"]
        if not ctx.get("country_group"):
            ctx["country_group"] = region["country_group"]
        if not ctx.get("industry_group"):
            igroup, _low = self._segment_matcher().industry_group(ctx.get("industry"))
            ctx["industry_group"] = igroup
        if not ctx.get("industry_insight"):
            ctx["industry_insight"] = self._segment_matcher().industry_insight(
                ctx["industry_group"]
            )

        # ── Guarantee no None/empty values reach templates ────────────
        # Jinja2's default() filter (without boolean=true) only catches
        # Undefined, not None. We backstop here so "Dear None" never happens.
        if not ctx.get("contact_name"):
            ctx["contact_name"] = "Valued Customer"
        if not ctx.get("company_name"):
            ctx["company_name"] = "your company"
        if not ctx.get("country"):
            ctx["country"] = ""
        if not ctx.get("industry"):
            ctx["industry"] = "industrial supply"

        return ctx


# ═══════════════════════════════════════════════════════════════════════════
# Template Loader — reads templates.json, provides rotation
# ═══════════════════════════════════════════════════════════════════════════


# TODO(future): dual template systems — TemplateLoader (JSON, used by
# batch/followup/UI) vs crm/manager.TemplateManager (DB, used by API).
# Unify to a single source of truth before growing either system.
class TemplateLoader:
    """Loads email templates from a JSON file and provides rotation.

    Usage:
        loader = TemplateLoader()
        t = loader.pick("general")  # least-recently-used general template
        subject = loader.renderer.render_subject(t["subject"], company_name="ACME")
        body = loader.renderer.render_body(t["body"], company_name="ACME")
        loader.mark_used(t["id"])
    """

    def __init__(
        self,
        templates_path: Optional[Path] = None,
        sender_company: str = "Example Operations",
    ):
        self.templates_path = templates_path or DEFAULT_TEMPLATES_PATH
        self.renderer = EmailTemplateRenderer(sender_company=sender_company)
        self._templates: list[dict] = []
        self._usage: dict[str, int] = {}  # template_id → times used
        self._last_used: dict[str, str] = {}  # template_id → ISO timestamp
        self._load()

    def _segment_matcher(self):
        """Lazy SegmentMatcher shared with the renderer."""
        return self.renderer._segment_matcher()

    # ── Loading ──────────────────────────────────────────────────────────

    def _load(self):
        """Load templates from the JSON file."""
        if not self.templates_path.exists():
            logger.warning(
                f"Templates file not found: {self.templates_path}. "
                f"Create templates/email/templates.json with your email templates."
            )
            self._templates = []
            return

        try:
            with open(self.templates_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._templates = data.get("templates", [])
            logger.info(
                f"Loaded {len(self._templates)} templates from {self.templates_path}"
            )
        except Exception as e:
            logger.error(f"Failed to load templates: {e}")
            self._templates = []

    def reload(self):
        """Reload templates from disk (useful after editing)."""
        self._load()
        self._usage = {}
        self._last_used = {}

    # ── Rotation ─────────────────────────────────────────────────────────

    def pick(self, category: str = "general") -> Optional[dict]:
        """Pick the least-recently-used template in a category.

        Rotation strategy:
          1. Templates never used → highest priority
          2. Templates used least recently → next priority
          3. Within same last-used time, prefer fewer total uses

        Returns the template dict with 'id', 'name', 'subject', 'body', 'category',
        or None if no template matches the category.
        """
        candidates = [t for t in self._templates if t.get("category") == category]

        if not candidates:
            # Fallback: any template
            candidates = self._templates
            if not candidates:
                logger.error("No templates available")
                return None

        if len(candidates) == 1:
            return candidates[0]

        # Sort by: never-used first, then least-recently-used
        def sort_key(t: dict) -> tuple:
            tid = t["id"]
            used = tid in self._last_used
            last = self._last_used.get(tid, "0000")
            count = self._usage.get(tid, 0)
            # Never used → (0, "0000", 0) sorts first
            # Used → (1, last_time, count) — earlier last_time sorts first
            return (0 if not used else 1, last, count)

        candidates.sort(key=sort_key)
        return candidates[0]

    def mark_used(self, template_id: str):
        """Record that a template was used (for rotation tracking)."""
        from datetime import datetime, timezone
        self._usage[template_id] = self._usage.get(template_id, 0) + 1
        self._last_used[template_id] = datetime.now(timezone.utc).isoformat()

    def pick_for_lead(self, lead: dict, category: str = "general") -> Optional[dict]:
        """Pick the least-recently-used template matching a lead's industry group.

        Selection chain:
          1. Templates whose industry_group equals the lead's mapped group
          2. General-group templates (fallback)
          3. Any template in the category (safety net)

        Country only affects rendered variables (greeting/signoff/region_name),
        which are injected automatically by EmailTemplateRenderer.
        """
        matcher = self._segment_matcher()
        group, _low = matcher.industry_group(lead.get("industry") if lead else None)

        candidates = [
            t for t in self._templates
            if t.get("category") == category
            and t.get("industry_group") == group
        ]
        if not candidates:
            candidates = [
                t for t in self._templates
                if t.get("category") == category
                and t.get("industry_group") == "general"
            ]
        if not candidates:
            candidates = [
                t for t in self._templates if t.get("category") == category
            ]

        return self._pick_lru(candidates)

    def _pick_lru(self, candidates: list[dict]) -> Optional[dict]:
        """Least-recently-used rotation over a candidate list."""
        if not candidates:
            logger.error("No templates available")
            return None
        if len(candidates) == 1:
            return candidates[0]

        def sort_key(t: dict) -> tuple:
            tid = t["id"]
            used = tid in self._last_used
            last = self._last_used.get(tid, "0000")
            count = self._usage.get(tid, 0)
            return (0 if not used else 1, last, count)

        candidates.sort(key=sort_key)
        return candidates[0]

    # ── Rendering helpers ────────────────────────────────────────────────

    def render_email(
        self,
        template: dict,
        company_name: Optional[str] = None,
        contact_name: Optional[str] = None,
        country: Optional[str] = None,
        industry: Optional[str] = None,
        source: Optional[str] = None,
        **extra,
    ) -> dict:
        """Render a full email (subject + body) from a template dict.

        Returns {"subject": str, "body_html": str, "template_id": str}.
        """
        subject = self.renderer.render_subject(
            template.get("subject", ""),
            company_name=company_name,
            contact_name=contact_name,
            country=country,
            industry=industry,
            source=source,
            **extra,
        )
        body = self.renderer.render_body(
            template.get("body", ""),
            company_name=company_name,
            contact_name=contact_name,
            country=country,
            industry=industry,
            source=source,
            **extra,
        )
        return {
            "subject": subject,
            "body_html": body,
            "template_id": template.get("id", "unknown"),
            "template_name": template.get("name", "unknown"),
            "category": template.get("category", "general"),
        }

    def preview_all(self, company_name: str = "ACME Corp", contact_name: str = "John Smith") -> list[dict]:
        """Preview all loaded templates with sample data."""
        previews = []
        _SAMPLE_INDUSTRY = {
            "automotive": "Automotive & Vehicles",
            "machinery": "Machinery",
            "construction": "Building Materials",
            "electronics": "Electronics & Electrical Equipment",
            "new_energy": "Solar & New Energy",
            "medical": "Medical Devices",
            "hardware_furniture": "Hardware",
            "general": "Industrial Supply",
        }
        for t in self._templates:
            rendered = self.render_email(
                t,
                company_name=company_name,
                contact_name=contact_name,
                country="USA",
                industry=_SAMPLE_INDUSTRY.get(
                    t.get("industry_group", "general"),
                    "Industrial Supply",
                ),
            )
            previews.append({
                "id": t["id"],
                "name": t["name"],
                "category": t["category"],
                "subject": rendered["subject"],
                "body_preview": rendered["body_html"][:200] + "...",
                "usage_count": self._usage.get(t["id"], 0),
                "last_used": self._last_used.get(t["id"]),
            })
        return previews

    # ── Queries ──────────────────────────────────────────────────────────

    @property
    def template_count(self) -> int:
        return len(self._templates)

    @property
    def categories(self) -> list[str]:
        cats = set(t.get("category", "general") for t in self._templates)
        return sorted(cats)

    @property
    def templates(self) -> list[dict]:
        """Return shallow template metadata/content copies for read-only callers."""
        return [dict(template) for template in self._templates]

    def get_by_id(self, template_id: str) -> Optional[dict]:
        for t in self._templates:
            if t.get("id") == template_id:
                return t
        return None

    def get_usage_stats(self) -> dict:
        return {
            "total_templates": len(self._templates),
            "categories": self.categories,
            "usage_by_template": {
                t["id"]: {
                    "name": t["name"],
                    "category": t["category"],
                    "times_used": self._usage.get(t["id"], 0),
                    "last_used": self._last_used.get(t["id"]),
                }
                for t in self._templates
            },
        }


# ---------------------------------------------------------------------------
# DB sync: JSON templates are canonical; email_templates mirrors them.
# ---------------------------------------------------------------------------


def sync_templates_to_db(db_path: Optional[Path] = None) -> dict:
    """Upsert JSON templates into the DB email_templates table (idempotent).

    Each JSON template is matched by its id stored in the template_key column.
    Rows previously synced but removed from JSON are deactivated, and legacy
    seeded templates (template_key NULL, zero usage) are deactivated so the
    UI/API single-send list matches the JSON library.

    Returns {"inserted": int, "updated": int, "deactivated": int}.
    """
    import sqlite3

    target = Path(db_path) if db_path else config.DB_PATH
    if not target.exists():
        raise FileNotFoundError(f"Database not found at {target}")

    loader = TemplateLoader()
    json_ids = [t["id"] for t in loader._templates]

    conn = sqlite3.connect(str(target))
    try:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(email_templates)").fetchall()
        }
        if "template_key" not in cols:
            conn.execute("ALTER TABLE email_templates ADD COLUMN template_key TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_templates_key "
            "ON email_templates(template_key)"
        )

        inserted = 0
        updated = 0
        now = conn.execute("SELECT datetime('now')").fetchone()[0]
        for t in loader._templates:
            existing = conn.execute(
                "SELECT id FROM email_templates WHERE template_key = ?",
                (t["id"],),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE email_templates SET
                       name=?, subject_template=?, body_template=?, description=?,
                       category=?, language='en', is_active=1, updated_at=?
                       WHERE id=?""",
                    (
                        t.get("name", ""),
                        t.get("subject", ""),
                        t.get("body", ""),
                        t.get("name", ""),
                        t.get("category", "general"),
                        now,
                        existing[0],
                    ),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO email_templates
                       (template_key, name, subject_template, body_template,
                        description, category, language, is_active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'en', 1, ?, ?)""",
                    (
                        t["id"],
                        t.get("name", ""),
                        t.get("subject", ""),
                        t.get("body", ""),
                        t.get("name", ""),
                        t.get("category", "general"),
                        now,
                        now,
                    ),
                )
                inserted += 1

        deactivated = 0
        if json_ids:
            placeholders = ",".join("?" * len(json_ids))
            cur = conn.execute(
                f"""UPDATE email_templates SET is_active=0, updated_at=?
                    WHERE template_key IS NOT NULL
                      AND template_key NOT IN ({placeholders})""",
                [now] + json_ids,
            )
            deactivated += cur.rowcount

        try:
            from crm.db import DEFAULT_TEMPLATES
            legacy_names = [d["name"] for d in DEFAULT_TEMPLATES]
            if legacy_names:
                placeholders = ",".join("?" * len(legacy_names))
                cur = conn.execute(
                    f"""UPDATE email_templates SET is_active=0, updated_at=?
                        WHERE template_key IS NULL AND usage_count = 0
                          AND name IN ({placeholders})""",
                    [now] + legacy_names,
                )
                deactivated += cur.rowcount
        except ImportError:
            pass

        conn.commit()
        logger.info(
            "Synced %d templates: %d inserted, %d updated, %d deactivated",
            len(json_ids), inserted, updated, deactivated,
        )
        return {
            "total": len(json_ids),
            "inserted": inserted,
            "updated": updated,
            "deactivated": deactivated,
        }
    finally:
        conn.close()
