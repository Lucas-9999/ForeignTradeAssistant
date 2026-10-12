"""
CRM Manager — Lead and Customer CRUD with business logic.

Pattern: mirrors PriceGod's CustomerProfileManager:
- Constructor takes `conn: sqlite3.Connection`
- _ensure_tables() for idempotent initialization
- Dataclass-based create/read/update/delete methods
- Row → dataclass conversion helpers
"""

import json
import logging
import sqlite3
from datetime import date, timedelta
from typing import Optional

from crm.models import (
    Lead,
    Customer,
    EmailTemplate,
    Communication,
    BlacklistEntry,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# LEAD MANAGER
# ═══════════════════════════════════════════════════════════════════════════


class LeadManager:
    """CRUD + business logic for leads."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── Create ──────────────────────────────────────────────────────────

    def create(self, lead: Lead) -> Lead:
        """Create a new lead. Auto-detects duplicates by email, then phone+company."""
        existing = self._find_duplicate(lead)
        if existing:
            logger.info(
                f"Duplicate detected: {lead.email or lead.company_name} "
                f"— marking as duplicate of #{existing.id}"
            )
            lead.duplicate_of = existing.id
            lead.status = "dead"

        cursor = self.conn.execute(
            """INSERT INTO leads
               (company_name, contact_name, email, phone, whatsapp, website,
                source, source_url, source_detail, industry, country, notes,
                status, duplicate_of, discovered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                lead.company_name,
                lead.contact_name,
                lead.email,
                lead.phone,
                lead.whatsapp,
                lead.website,
                lead.source,
                lead.source_url,
                lead.source_detail,
                lead.industry,
                lead.country,
                lead.notes,
                lead.status,
                lead.duplicate_of,
            ),
        )
        self.conn.commit()
        lead.id = cursor.lastrowid
        logger.info(f"Created lead #{lead.id}: {lead.company_name} ({lead.email})")
        return lead

    def create_batch(self, leads: list[Lead]) -> tuple[int, int]:
        """Bulk create leads with batch dedup + single commit.
        # TODO(future): dedup is layered 3× — pipeline in-batch dedup, this
        # method, and create()/_find_duplicate. Consolidate into one path.

        Previously did individual commits per lead (~0.5s/lead on SQLite),
        which made 40K-lead ingestion take hours. Now:
          1. Collect all emails → one IN() query against DB
          2. Filter out duplicates in memory
          3. Batch INSERT all remaining leads via executemany()
          4. Single COMMIT

        Returns (created, duplicates_skipped).
        """
        if not leads:
            return 0, 0

        # ── Step 1: batch email lookup ──────────────────────────────────
        emails = [lead.email.strip().lower() for lead in leads
                  if lead.email and lead.email.strip()]
        existing_emails: set[str] = set()
        chunk_size = 500  # SQLite IN() param limit safety
        if emails:
            for i in range(0, len(emails), chunk_size):
                chunk = emails[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.conn.execute(
                    f"SELECT LOWER(email) FROM leads WHERE LOWER(email) IN ({placeholders})",
                    chunk,
                ).fetchall()
                existing_emails.update(r[0] for r in rows if r[0])

        # ── Step 1b: batch buyer_id lookup (寻客星权威身份键) ──────────
        # The platform-stable buyer_id (stored in source_detail JSON) is a
        # stronger identity than company+phone for email-less leads — same
        # platform record repeatedly re-fetched would otherwise slip through.
        buyer_keys = [b for b in (self._buyer_id(lead) for lead in leads) if b]
        existing_buyers: set[str] = set()
        if buyer_keys:
            for i in range(0, len(buyer_keys), chunk_size):
                chunk = buyer_keys[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = self.conn.execute(
                    f"""SELECT CAST(json_extract(source_detail, '$.buyer_id') AS TEXT)
                        FROM leads
                        WHERE json_extract(source_detail, '$.buyer_id') IS NOT NULL
                          AND CAST(json_extract(source_detail, '$.buyer_id') AS TEXT)
                              IN ({placeholders})""",
                    chunk,
                ).fetchall()
                existing_buyers.update(r[0] for r in rows if r[0])

        # ── Step 2: filter duplicates + collect company/phone keys ──────
        # For leads without email, check company_name + phone
        new_leads: list[Lead] = []
        skipped = 0
        # In-batch seen keys — dedup against earlier rows in this same batch
        batch_emails: set[str] = set()
        batch_phone_keys: set[tuple[str, str]] = set()
        batch_buyers: set[str] = set()

        # Batch lookup for company+phone fallback
        phone_keys = [
            (lead.company_name.strip().lower(), lead.phone.strip())
            for lead in leads
            if lead.company_name and lead.phone and not (
                lead.email and lead.email.strip().lower() in existing_emails
            )
        ]
        existing_phones: set[tuple[str, str]] = set()
        if phone_keys:
            for i in range(0, len(phone_keys), chunk_size):
                chunk = phone_keys[i:i + chunk_size]
                placeholders = ",".join("(? ,?)" for _ in chunk)
                flat = [v for pair in chunk for v in pair]
                rows = self.conn.execute(
                    f"SELECT LOWER(company_name), phone FROM leads "
                    f"WHERE (LOWER(company_name), phone) IN ({placeholders})",
                    flat,
                ).fetchall()
                existing_phones.update(
                    (r[0], r[1].strip() if r[1] else "") for r in rows
                )

        for lead in leads:
            email_key = (
                lead.email.strip().lower()
                if lead.email and lead.email.strip()
                else None
            )
            # Email check — against DB and against earlier rows in this batch
            if email_key and (
                email_key in existing_emails or email_key in batch_emails
            ):
                skipped += 1
                continue
            # Buyer ID check — against DB and against this batch
            bid = self._buyer_id(lead)
            if bid and (bid in existing_buyers or bid in batch_buyers):
                skipped += 1
                continue
            # Company + phone check — against DB and against this batch
            if lead.company_name and lead.phone:
                key = (lead.company_name.strip().lower(), lead.phone.strip())
                if key in existing_phones or key in batch_phone_keys:
                    skipped += 1
                    continue
            new_leads.append(lead)
            if email_key:
                batch_emails.add(email_key)
            if bid:
                batch_buyers.add(bid)
            if lead.company_name and lead.phone:
                batch_phone_keys.add(
                    (lead.company_name.strip().lower(), lead.phone.strip())
                )

        if not new_leads:
            logger.info(
                f"Batch lead creation: 0 created, {skipped} duplicates skipped "
                f"(all {len(leads)} leads were duplicates)"
            )
            return 0, skipped

        # ── Step 3: batch insert ─────────────────────────────────────────
        rows_data = [
            (
                lead.company_name,
                lead.contact_name,
                lead.email,
                lead.phone,
                lead.whatsapp,
                lead.website,
                lead.source,
                lead.source_url,
                lead.source_detail,
                lead.industry,
                lead.country,
                lead.notes,
                lead.status,
                lead.duplicate_of,
            )
            for lead in new_leads
        ]

        cursor = self.conn.executemany(
            """INSERT INTO leads
               (company_name, contact_name, email, phone, whatsapp, website,
                source, source_url, source_detail, industry, country, notes,
                status, duplicate_of, discovered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            rows_data,
        )
        self.conn.commit()

        created = cursor.rowcount
        # Assign IDs to the lead objects (first ID = lastrowid - created + 1)
        # Guard: when ALL leads are duplicates, executemany produces 0 rows
        # and cursor.lastrowid is None (SQLite spec — no row inserted).
        # Skip ID assignment in that case; leads already have duplicate_of set.
        if created > 0 and cursor.lastrowid is not None:
            first_id = cursor.lastrowid - created + 1
            for i, lead in enumerate(new_leads):
                lead.id = first_id + i

        logger.info(
            f"Batch lead creation: {created} created, {skipped} duplicates skipped"
        )
        return created, skipped

    # ── Read ────────────────────────────────────────────────────────────

    def get_by_id(self, lead_id: int) -> Optional[Lead]:
        """Get a lead by ID."""
        row = self.conn.execute(
            "SELECT * FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        return self._row_to_lead(row) if row else None

    def find_by_email(self, email: str) -> Optional[Lead]:
        """Find a lead by email (case-insensitive)."""
        row = self.conn.execute(
            "SELECT * FROM leads WHERE LOWER(email) = LOWER(?)", (email,)
        ).fetchone()
        return self._row_to_lead(row) if row else None

    def list_all(
        self,
        status: Optional[str] = None,
        source: Optional[str] = None,
        search: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[Lead]:
        """List leads with optional filters."""
        conditions = []
        params = []

        if status:
            conditions.append("status = ?")
            params.append(status)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if search:
            conditions.append(
                "(company_name LIKE ? OR contact_name LIKE ? OR email LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like])

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM leads WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_lead(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        """Count leads grouped by status."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM leads GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    # ── Update ──────────────────────────────────────────────────────────

    def update(self, lead: Lead) -> None:
        """Update an existing lead."""
        if not lead.id:
            raise ValueError("Cannot update lead without id")
        self.conn.execute(
            """UPDATE leads SET
               company_name=?, contact_name=?, email=?, phone=?, whatsapp=?,
               website=?, source=?, source_url=?, industry=?, country=?,
               notes=?, status=?, duplicate_of=?,
               updated_at=datetime('now')
               WHERE id=?""",
            (
                lead.company_name,
                lead.contact_name,
                lead.email,
                lead.phone,
                lead.whatsapp,
                lead.website,
                lead.source,
                lead.source_url,
                lead.industry,
                lead.country,
                lead.notes,
                lead.status,
                lead.duplicate_of,
                lead.id,
            ),
        )
        self.conn.commit()
        logger.info(f"Updated lead #{lead.id}")

    def mark_contacted(self, lead_id: int) -> None:
        """Mark a lead as contacted and update timestamps."""
        self.conn.execute(
            """UPDATE leads SET
               status = 'contacted',
               first_contacted_at = COALESCE(first_contacted_at, datetime('now')),
               last_contacted_at = datetime('now'),
               updated_at = datetime('now')
               WHERE id = ?""",
            (lead_id,),
        )
        self.conn.commit()

    def record_manual_contact(
        self, lead_id: int, new_status: str = "contacted", contacted_at: Optional[str] = None
    ) -> None:
        """Tracker touch after a manual (Zoho) follow-up.

        Only promotes status 'new' -> new_status; 'replied'/'customer'/'dead'
        keep their status (we never resurrect a dead lead or downgrade a
        replied one). first/last_contacted_at are always updated — defaulting
        to now, or to `contacted_at` ("YYYY-MM-DD HH:MM:SS" UTC) when the caller
        knows the actual send time of the follow-up.
        """
        self.conn.execute(
            """UPDATE leads SET
               status = CASE WHEN status = 'new' THEN ? ELSE status END,
               first_contacted_at = COALESCE(first_contacted_at, COALESCE(?, datetime('now'))),
               last_contacted_at = COALESCE(?, datetime('now')),
               updated_at = datetime('now')
               WHERE id = ?""",
            (new_status, contacted_at, contacted_at, lead_id),
        )
        self.conn.commit()

    def mark_replied(self, lead_id: int) -> None:
        """Mark a lead as having replied."""
        self.conn.execute(
            """UPDATE leads SET
               status = 'replied',
               last_contacted_at = datetime('now'),
               updated_at = datetime('now')
               WHERE id = ?""",
            (lead_id,),
        )
        self.conn.commit()

    def mark_dead(self, lead_id: int) -> None:
        """Mark a lead as dead (bounced, unsubscribed, or invalid)."""
        self.conn.execute(
            """UPDATE leads SET
               status = 'dead',
               updated_at = datetime('now')
               WHERE id = ?""",
            (lead_id,),
        )
        self.conn.commit()

    # ── Delete ──────────────────────────────────────────────────────────

    def delete(self, lead_id: int) -> None:
        """Delete a lead and its communications."""
        self.conn.execute(
            "DELETE FROM communications WHERE lead_id = ?", (lead_id,)
        )
        self.conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        self.conn.commit()
        logger.info(f"Deleted lead #{lead_id}")

    # ── Promotions ──────────────────────────────────────────────────────

    def promote_to_customer(self, lead_id: int) -> Optional[int]:
        """Convert a lead into a customer. Returns the new customer_id."""
        lead = self.get_by_id(lead_id)
        if not lead:
            logger.warning(f"Cannot promote: lead #{lead_id} not found")
            return None

        cust_mgr = CustomerManager(self.conn)
        customer = Customer(
            lead_id=lead.id,
            company_name=lead.company_name or "Unknown",
            contact_name=lead.contact_name,
            email=lead.email,
            phone=lead.phone,
            whatsapp=lead.whatsapp,
            country=lead.country,
            industry=lead.industry,
        )
        customer = cust_mgr.create(customer)

        # Update lead status
        self.conn.execute(
            "UPDATE leads SET status='customer', updated_at=datetime('now') WHERE id=?",
            (lead_id,),
        )
        self.conn.commit()
        logger.info(f"Promoted lead #{lead_id} → customer #{customer.id}")
        return customer.id

    # ── Dedup ───────────────────────────────────────────────────────────

    @staticmethod
    def _buyer_id(lead: Lead) -> Optional[str]:
        """Extract 寻客星 buyer_id from source_detail JSON as str, or None."""
        if not lead.source_detail:
            return None
        try:
            d = json.loads(lead.source_detail)
        except Exception:
            return None
        bid = d.get("buyer_id")
        return str(bid) if bid is not None else None

    def _find_duplicate(self, lead: Lead) -> Optional[Lead]:
        """Find an existing lead that matches the given lead.

        Strategy (in order of confidence):
        1. Email match (highest confidence — email is unique per business contact)
        2. 寻客星 buyer_id match (platform-stable identity stored in source_detail)
        3. Company name + phone match (fallback for leads without email)
           Only triggers when both values are non-empty, to avoid false positives
           from generic company names with empty phones.
        """
        # 1) Email match
        if lead.email:
            existing = self.find_by_email(lead.email)
            if existing:
                return existing

        # 1b) 寻客星 buyer_id 权威身份匹配 (仅来源带 buyer_id 时生效)
        bid = self._buyer_id(lead)
        if bid:
            row = self.conn.execute(
                """SELECT * FROM leads
                   WHERE json_extract(source_detail, '$.buyer_id') IS NOT NULL
                     AND CAST(json_extract(source_detail, '$.buyer_id') AS TEXT) = ?
                     AND duplicate_of IS NULL
                     AND id != ?
                   LIMIT 1""",
                (bid, lead.id or 0),
            ).fetchone()
            if row:
                return self._row_to_lead(row)

        # 2) Company + phone fallback (only when both are present)
        if lead.company_name and lead.phone:
            row = self.conn.execute(
                """SELECT * FROM leads
                   WHERE company_name = ? AND phone = ?
                     AND duplicate_of IS NULL
                     AND id != ?
                   LIMIT 1""",
                (lead.company_name, lead.phone, lead.id or 0),
            ).fetchone()
            if row:
                return self._row_to_lead(row)

        return None

    def find_duplicates(self) -> list[dict]:
        """Find leads with the same email. Returns list of {email, lead_ids, count}."""
        rows = self.conn.execute("""
            SELECT LOWER(email) as email_norm, COUNT(*) as cnt,
                   GROUP_CONCAT(id) as ids
            FROM leads
            WHERE email IS NOT NULL AND email != '' AND duplicate_of IS NULL
            GROUP BY LOWER(email)
            HAVING COUNT(*) > 1
        """).fetchall()
        return [
            {
                "email": r["email_norm"],
                "count": r["cnt"],
                "lead_ids": [int(i) for i in r["ids"].split(",")],
            }
            for r in rows
        ]

    def cleanup_duplicates(self, dry_run: bool = True) -> dict:
        """Merge existing duplicates (by company+phone, where email is missing).

        For each group of duplicates sharing the same company_name + phone,
        keeps the earliest (by id) and marks the rest with duplicate_of.
        Only targets leads without email (the email-based dedup handles those).

        Returns:
            dict with "groups_found", "leads_merged", "dry_run" keys.
        """
        # Find duplicate groups: same company_name + phone, no email
        rows = self.conn.execute("""
            SELECT company_name, phone, COUNT(*) as cnt,
                   MIN(id) as keep_id,
                   GROUP_CONCAT(id) as all_ids
            FROM leads
            WHERE duplicate_of IS NULL
              AND (email IS NULL OR email = '')
              AND company_name IS NOT NULL AND company_name != ''
              AND phone IS NOT NULL AND phone != ''
            GROUP BY company_name, phone
            HAVING COUNT(*) > 1
        """).fetchall()

        groups_found = len(rows)
        leads_merged = 0

        for r in rows:
            keep_id = r["keep_id"]
            all_ids = [int(i) for i in r["all_ids"].split(",")]
            merge_ids = [i for i in all_ids if i != keep_id]

            if not dry_run:
                for merge_id in merge_ids:
                    self.merge_duplicates(keep_id, merge_id)
            leads_merged += len(merge_ids)

        if dry_run:
            logger.info(
                f"[DRY RUN] Found {groups_found} duplicate groups, "
                f"would merge {leads_merged} leads"
            )
        else:
            logger.info(
                f"Cleaned up {leads_merged} duplicate leads across "
                f"{groups_found} groups"
            )

        return {
            "groups_found": groups_found,
            "leads_merged": leads_merged,
            "dry_run": dry_run,
        }

    def merge_duplicates(self, keep_id: int, merge_id: int) -> None:
        """Merge merge_id into keep_id, update communications, mark duplicate."""
        self.conn.execute(
            "UPDATE communications SET lead_id=? WHERE lead_id=?",
            (keep_id, merge_id),
        )
        self.conn.execute(
            "UPDATE leads SET duplicate_of=?, status='dead', updated_at=datetime('now') WHERE id=?",
            (keep_id, merge_id),
        )
        self.conn.commit()
        logger.info(f"Merged lead #{merge_id} into #{keep_id}")

    # ── Helpers ─────────────────────────────────────────────────────────

    def _row_to_lead(self, row: sqlite3.Row) -> Lead:
        """Convert a DB row to a Lead dataclass."""
        return Lead(
            id=row["id"],
            company_name=row["company_name"],
            contact_name=row["contact_name"],
            email=row["email"],
            phone=row["phone"],
            whatsapp=row["whatsapp"],
            website=row["website"],
            source=row["source"],
            source_url=row["source_url"],
            source_detail=row["source_detail"],
            industry=row["industry"],
            country=row["country"],
            notes=row["notes"],
            status=row["status"],
            duplicate_of=row["duplicate_of"],
            discovered_at=row["discovered_at"],
            first_contacted_at=row["first_contacted_at"],
            last_contacted_at=row["last_contacted_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMER MANAGER
# ═══════════════════════════════════════════════════════════════════════════


class CustomerManager:
    """CRUD + business logic for customers. Mirrors PriceGod's CustomerProfileManager."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── Create ──────────────────────────────────────────────────────────

    def create(self, customer: Customer) -> Customer:
        """Create a new customer."""
        # Calculate next follow-up date
        followup_date = date.today() + timedelta(days=customer.followup_interval_days)

        cursor = self.conn.execute(
            """INSERT INTO customers
               (lead_id, company_name, contact_name, email, phone, whatsapp,
                country, industry, preferred_channel, followup_interval_days,
                next_followup_at, notes, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                customer.lead_id,
                customer.company_name,
                customer.contact_name,
                customer.email,
                customer.phone,
                customer.whatsapp,
                customer.country,
                customer.industry,
                customer.preferred_channel,
                customer.followup_interval_days,
                followup_date.isoformat(),
                customer.notes,
                customer.tags,
            ),
        )
        self.conn.commit()
        customer.id = cursor.lastrowid
        customer.next_followup_at = followup_date.isoformat()
        logger.info(f"Created customer #{customer.id}: {customer.company_name}")
        return customer

    # ── Read ────────────────────────────────────────────────────────────

    def get_by_id(self, customer_id: int) -> Optional[Customer]:
        """Get a customer by ID."""
        row = self.conn.execute(
            "SELECT * FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        return self._row_to_customer(row) if row else None

    def list_all(
        self,
        search: Optional[str] = None,
        is_active: Optional[int] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> list[Customer]:
        """List customers with optional filters."""
        conditions = []
        params = []

        if is_active is not None:
            conditions.append("is_active = ?")
            params.append(is_active)
        if search:
            conditions.append(
                "(company_name LIKE ? OR contact_name LIKE ? OR email LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like])

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            f"SELECT * FROM customers WHERE {where} "
            f"ORDER BY next_followup_at ASC NULLS LAST LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_customer(r) for r in rows]

    def get_due_followups(self, days_ahead: int = 7) -> list[Customer]:
        """Get customers whose follow-up is due within `days_ahead` days."""
        cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
        today = date.today().isoformat()
        rows = self.conn.execute(
            """SELECT * FROM customers
               WHERE is_active = 1
               AND next_followup_at IS NOT NULL
               AND next_followup_at <= ?
               AND next_followup_at >= ?
               ORDER BY next_followup_at ASC""",
            (cutoff, today),
        ).fetchall()
        return [self._row_to_customer(r) for r in rows]

    def get_overdue_followups(self) -> list[Customer]:
        """Get customers whose follow-up is overdue."""
        today = date.today().isoformat()
        rows = self.conn.execute(
            """SELECT * FROM customers
               WHERE is_active = 1
               AND next_followup_at < ?
               ORDER BY next_followup_at ASC""",
            (today,),
        ).fetchall()
        return [self._row_to_customer(r) for r in rows]

    def get_communication_history(self, customer_id: int) -> list[dict]:
        """Get all communications for a customer (via their lead_id too)."""
        customer = self.get_by_id(customer_id)
        if not customer:
            return []

        rows = self.conn.execute(
            """SELECT * FROM communications
               WHERE customer_id = ? OR lead_id = ?
               ORDER BY sent_at DESC""",
            (customer_id, customer.lead_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Update ──────────────────────────────────────────────────────────

    def update(self, customer: Customer) -> None:
        """Update an existing customer."""
        if not customer.id:
            raise ValueError("Cannot update customer without id")
        self.conn.execute(
            """UPDATE customers SET
               company_name=?, contact_name=?, email=?, phone=?, whatsapp=?,
               country=?, industry=?, preferred_channel=?, followup_interval_days=?,
               notes=?, tags=?, is_active=?,
               updated_at=datetime('now')
               WHERE id=?""",
            (
                customer.company_name,
                customer.contact_name,
                customer.email,
                customer.phone,
                customer.whatsapp,
                customer.country,
                customer.industry,
                customer.preferred_channel,
                customer.followup_interval_days,
                customer.notes,
                customer.tags,
                customer.is_active,
                customer.id,
            ),
        )
        self.conn.commit()
        logger.info(f"Updated customer #{customer.id}")

    def record_contact(self, customer_id: int) -> None:
        """Update follow-up dates after a successful contact."""
        followup_date = date.today() + timedelta(days=30)  # default
        self.conn.execute(
            """UPDATE customers SET
               last_contacted_at = datetime('now'),
               next_followup_at = ?,
               updated_at = datetime('now')
               WHERE id = ?""",
            (followup_date.isoformat(), customer_id),
        )
        self.conn.commit()

    # ── Delete ──────────────────────────────────────────────────────────

    def delete(self, customer_id: int) -> None:
        """Soft-delete: set is_active=0."""
        self.conn.execute(
            "UPDATE customers SET is_active=0, updated_at=datetime('now') WHERE id=?",
            (customer_id,),
        )
        self.conn.commit()
        logger.info(f"Soft-deleted customer #{customer_id}")

    # ── Helpers ─────────────────────────────────────────────────────────

    def _row_to_customer(self, row: sqlite3.Row) -> Customer:
        """Convert a DB row to a Customer dataclass."""
        return Customer(
            id=row["id"],
            lead_id=row["lead_id"],
            company_name=row["company_name"],
            contact_name=row["contact_name"],
            email=row["email"],
            phone=row["phone"],
            whatsapp=row["whatsapp"],
            country=row["country"],
            industry=row["industry"],
            preferred_channel=row["preferred_channel"],
            followup_interval_days=row["followup_interval_days"],
            last_contacted_at=row["last_contacted_at"],
            next_followup_at=row["next_followup_at"],
            total_orders=row["total_orders"],
            notes=row["notes"],
            tags=row["tags"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# TEMPLATE MANAGER
# ═══════════════════════════════════════════════════════════════════════════


# TODO(future): dual template systems — TemplateManager (DB, used by API)
# vs emailer/templates.TemplateLoader (JSON, used by batch/followup/UI).
# Unify to a single source of truth.
class TemplateManager:
    """CRUD + rotation logic for email templates."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, tmpl: EmailTemplate) -> EmailTemplate:
        cursor = self.conn.execute(
            """INSERT INTO email_templates
               (name, subject_template, body_template, description, category, language)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (tmpl.name, tmpl.subject_template, tmpl.body_template,
             tmpl.description, tmpl.category, tmpl.language),
        )
        self.conn.commit()
        tmpl.id = cursor.lastrowid
        return tmpl

    def get_by_id(self, template_id: int) -> Optional[EmailTemplate]:
        row = self.conn.execute(
            "SELECT * FROM email_templates WHERE id = ?", (template_id,)
        ).fetchone()
        return self._row_to_template(row) if row else None

    def list_active(self, category: Optional[str] = None) -> list[EmailTemplate]:
        conditions = ["is_active = 1"]
        params = []
        if category:
            conditions.append("category = ?")
            params.append(category)

        where = " AND ".join(conditions)
        rows = self.conn.execute(
            f"SELECT * FROM email_templates WHERE {where} ORDER BY name"
        ).fetchall()
        return [self._row_to_template(r) for r in rows]

    def select_for_rotation(self, category: str = "icebreaker") -> Optional[EmailTemplate]:
        """Pick the least-recently-used active template in a category.

        This is the core of the template rotation anti-spam strategy.
        """
        row = self.conn.execute(
            """SELECT * FROM email_templates
               WHERE category = ? AND is_active = 1
               ORDER BY last_used_at ASC NULLS FIRST, usage_count ASC
               LIMIT 1""",
            (category,),
        ).fetchone()
        return self._row_to_template(row) if row else None

    def record_usage(self, template_id: int) -> None:
        """Increment usage counter and update last_used_at."""
        self.conn.execute(
            """UPDATE email_templates
               SET usage_count = usage_count + 1,
                   last_used_at = datetime('now'),
                   updated_at = datetime('now')
               WHERE id = ?""",
            (template_id,),
        )
        self.conn.commit()

    def update(self, tmpl: EmailTemplate) -> None:
        if not tmpl.id:
            raise ValueError("Cannot update template without id")
        self.conn.execute(
            """UPDATE email_templates SET
               name=?, subject_template=?, body_template=?, description=?,
               category=?, language=?, is_active=?,
               updated_at=datetime('now')
               WHERE id=?""",
            (tmpl.name, tmpl.subject_template, tmpl.body_template,
             tmpl.description, tmpl.category, tmpl.language, tmpl.is_active, tmpl.id),
        )
        self.conn.commit()

    def delete(self, template_id: int) -> None:
        self.conn.execute(
            "DELETE FROM email_templates WHERE id = ?", (template_id,)
        )
        self.conn.commit()

    def _row_to_template(self, row: sqlite3.Row) -> EmailTemplate:
        return EmailTemplate(
            id=row["id"],
            name=row["name"],
            subject_template=row["subject_template"],
            body_template=row["body_template"],
            description=row["description"],
            category=row["category"],
            language=row["language"],
            usage_count=row["usage_count"],
            last_used_at=row["last_used_at"],
            is_active=row["is_active"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# COMMUNICATION MANAGER
# ═══════════════════════════════════════════════════════════════════════════


class CommunicationManager:
    """Log and query communication history.

    Phase 2 (IMAP): supports reply tracking via message_id / in_reply_to.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def log(self, comm: Communication) -> Communication:
        cursor = self.conn.execute(
            """INSERT INTO communications
               (lead_id, customer_id, template_id, channel, direction,
                subject, body, status, error_message,
                message_id, in_reply_to, references_header, imap_uid,
                source, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
            (
                comm.lead_id,
                comm.customer_id,
                comm.template_id,
                comm.channel,
                comm.direction,
                comm.subject,
                comm.body,
                comm.status,
                comm.error_message,
                comm.message_id,
                comm.in_reply_to,
                comm.references_header,
                comm.imap_uid,
                comm.source,
                comm.sent_at,
            ),
        )
        self.conn.commit()
        comm.id = cursor.lastrowid
        return comm

    def update_status(self, comm_id: int, status: str, error: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE communications SET status=?, error_message=? WHERE id=?",
            (status, error, comm_id),
        )
        self.conn.commit()

    def mark_replied(self, comm_id: int) -> None:
        """Mark an outbound communication as having received a reply."""
        self.conn.execute(
            "UPDATE communications SET status='replied', replied_at=datetime('now') "
            "WHERE id=?",
            (comm_id,),
        )
        self.conn.commit()

    def get_by_message_id(self, message_id: str) -> Optional[dict]:
        """Look up an outbound communication by its Message-ID header.

        Used by IMAP inbox monitor to match incoming replies to sent emails.
        """
        row = self.conn.execute(
            "SELECT * FROM communications WHERE message_id=?",
            (message_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_latest_outbound_to(self, email: str) -> Optional[dict]:
        """Find the most recent outbound email to a given address.

        Fallback matching when In-Reply-To header is absent.
        Joins on leads.email to resolve recipient.
        """
        row = self.conn.execute(
            """SELECT c.* FROM communications c
               JOIN leads l ON l.id = c.lead_id
               WHERE l.email = ?
                 AND c.direction = 'outbound'
                 AND c.channel = 'email'
               ORDER BY c.sent_at DESC
               LIMIT 1""",
            (email,),
        ).fetchone()
        return dict(row) if row else None

    def imap_uid_exists(self, imap_uid: str) -> bool:
        """Check if an IMAP UID has already been processed (prevents duplicates)."""
        row = self.conn.execute(
            "SELECT 1 FROM communications WHERE imap_uid=? LIMIT 1",
            (imap_uid,),
        ).fetchone()
        return row is not None

    def get_by_lead(self, lead_id: int, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM communications WHERE lead_id=? ORDER BY sent_at DESC LIMIT ?",
            (lead_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_today(self) -> int:
        """Count emails sent today (for anti-spam quota)."""
        row = self.conn.execute(
            """SELECT COUNT(*) as cnt FROM communications
               WHERE channel='email' AND direction='outbound'
               AND date(sent_at) = date('now')"""
        ).fetchone()
        return row["cnt"] if row else 0

    def has_outbound_to(self, lead_id: int) -> bool:
        """Check if a lead has ever received an outbound email.

        Returns True if at least one outbound email (any status) exists for this lead.
        Used as the permanent dedup guard for first-contact (icebreaker) sends.
        """
        row = self.conn.execute(
            """SELECT 1 FROM communications
               WHERE lead_id = ?
                 AND channel = 'email'
                 AND direction = 'outbound'
               LIMIT 1""",
            (lead_id,),
        ).fetchone()
        return row is not None

    def has_outbound_to_email(self, email: str) -> bool:
        """Check if any lead with this email has ever received an outbound email.

        Handles the edge case where the same company email appears under
        multiple lead records (e.g. from different collection sources).
        """
        row = self.conn.execute(
            """SELECT 1 FROM communications c
               JOIN leads l ON l.id = c.lead_id
               WHERE LOWER(l.email) = LOWER(?)
                 AND c.channel = 'email'
                 AND c.direction = 'outbound'
               LIMIT 1""",
            (email,),
        ).fetchone()
        return row is not None

    def count_today_by_domain(self) -> dict[str, int]:
        """Count today's emails grouped by recipient domain."""
        rows = self.conn.execute("""
            SELECT
                LOWER(SUBSTR(l.email, INSTR(l.email, '@') + 1)) as domain,
                COUNT(*) as cnt
            FROM communications c
            JOIN leads l ON l.id = c.lead_id
            WHERE c.channel = 'email'
              AND c.direction = 'outbound'
              AND date(c.sent_at) = date('now')
              AND l.email IS NOT NULL
            GROUP BY domain
        """).fetchall()
        return {r["domain"]: r["cnt"] for r in rows if r["domain"]}


# ═══════════════════════════════════════════════════════════════════════════
# BLACKLIST MANAGER
# ═══════════════════════════════════════════════════════════════════════════


class BlacklistManager:
    """Manage the never-send list."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def is_blocked(self, email: str) -> bool:
        """Check if an email is blacklisted."""
        row = self.conn.execute(
            "SELECT 1 FROM blacklist WHERE LOWER(email) = LOWER(?)", (email,)
        ).fetchone()
        return row is not None

    def add(self, email: str, reason: str = "bounced") -> None:
        """Add an email to the blacklist. Idempotent — ignores duplicates."""
        try:
            self.conn.execute(
                "INSERT INTO blacklist (email, reason) VALUES (?, ?)",
                (email.strip().lower(), reason),
            )
            self.conn.commit()
            logger.info(f"Blacklisted: {email} ({reason})")
        except sqlite3.IntegrityError:
            pass  # already blacklisted

    def remove(self, email: str) -> None:
        self.conn.execute(
            "DELETE FROM blacklist WHERE LOWER(email) = LOWER(?)", (email,)
        )
        self.conn.commit()

    def list_all(self) -> list[BlacklistEntry]:
        rows = self.conn.execute(
            "SELECT * FROM blacklist ORDER BY created_at DESC"
        ).fetchall()
        return [
            BlacklistEntry(id=r["id"], email=r["email"], reason=r["reason"], created_at=r["created_at"])
            for r in rows
        ]
