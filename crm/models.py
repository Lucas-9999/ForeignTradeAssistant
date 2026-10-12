"""
CRM Dataclass Models — domain objects for the Foreign Trade Assistant.

Pattern: @dataclass with to_dict() serialization, matching PriceGod's approach.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Lead:
    """A prospect discovered via scraping or manual entry, not yet contacted."""

    id: Optional[int] = None
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    website: Optional[str] = None
    source: str = "manual"              # 'manual', 'facebook', 'industry_site', 'xunkexing'
    source_url: Optional[str] = None
    source_detail: Optional[str] = None  # JSON: extra source metadata
    industry: Optional[str] = None
    country: Optional[str] = None
    notes: Optional[str] = None
    status: str = "new"                  # 'new', 'contacted', 'replied', 'customer', 'dead'
    duplicate_of: Optional[int] = None   # points to canonical lead.id
    discovered_at: Optional[str] = None
    first_contacted_at: Optional[str] = None
    last_contacted_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "company_name": self.company_name,
            "contact_name": self.contact_name,
            "email": self.email,
            "phone": self.phone,
            "whatsapp": self.whatsapp,
            "website": self.website,
            "source": self.source,
            "source_url": self.source_url,
            "industry": self.industry,
            "country": self.country,
            "notes": self.notes,
            "status": self.status,
            "duplicate_of": self.duplicate_of,
            "discovered_at": self.discovered_at,
            "first_contacted_at": self.first_contacted_at,
            "last_contacted_at": self.last_contacted_at,
        }


@dataclass
class Customer:
    """A confirmed business contact — either promoted from a lead or manually added."""

    id: Optional[int] = None
    lead_id: Optional[int] = None       # origin lead, if promoted
    company_name: str = ""
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    country: Optional[str] = None
    industry: Optional[str] = None
    preferred_channel: str = "email"     # 'email', 'whatsapp', 'both'
    followup_interval_days: int = 30
    last_contacted_at: Optional[str] = None
    next_followup_at: Optional[str] = None
    total_orders: int = 0
    notes: Optional[str] = None
    tags: Optional[str] = None           # JSON array: ["vip", "urgent"]
    is_active: int = 1
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lead_id": self.lead_id,
            "company_name": self.company_name,
            "contact_name": self.contact_name,
            "email": self.email,
            "phone": self.phone,
            "whatsapp": self.whatsapp,
            "country": self.country,
            "industry": self.industry,
            "preferred_channel": self.preferred_channel,
            "followup_interval_days": self.followup_interval_days,
            "last_contacted_at": self.last_contacted_at,
            "next_followup_at": self.next_followup_at,
            "total_orders": self.total_orders,
            "notes": self.notes,
            "tags": self.tags,
            "is_active": self.is_active,
        }


@dataclass
class EmailTemplate:
    """A reusable email template with Jinja2 subject and body."""

    id: Optional[int] = None
    name: str = ""
    subject_template: str = ""           # Jinja2 template
    body_template: str = ""              # Jinja2 HTML template
    description: Optional[str] = None
    category: str = "icebreaker"         # 'icebreaker', 'followup', 'promotional', 'seasonal'
    language: str = "en"
    usage_count: int = 0                 # rotation counter
    last_used_at: Optional[str] = None
    is_active: int = 1
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "subject_template": self.subject_template,
            "body_template": self.body_template,
            "description": self.description,
            "category": self.category,
            "language": self.language,
            "usage_count": self.usage_count,
            "last_used_at": self.last_used_at,
            "is_active": self.is_active,
        }


@dataclass
class Communication:
    """Record of an outbound or inbound message.

    Reply-tracking fields (Phase 2 — IMAP inbox monitoring):
      - message_id:       RFC 2822 Message-ID of the SENT email (set by sender)
      - in_reply_to:      Message-ID this message is replying to (for inbound)
      - references_header: Threading References chain (for inbound)
      - imap_uid:         IMAP UID of fetched message (prevents re-processing)
    """

    id: Optional[int] = None
    lead_id: Optional[int] = None
    customer_id: Optional[int] = None
    template_id: Optional[int] = None
    channel: str = "email"               # 'email', 'whatsapp'
    direction: str = "outbound"          # 'outbound', 'inbound'
    subject: Optional[str] = None
    body: Optional[str] = None
    status: str = "pending"              # 'pending', 'sent', 'delivered', 'failed', 'bounced', 'replied'
    error_message: Optional[str] = None
    message_id: Optional[str] = None     # RFC 2822 Message-ID (set on sent email)
    in_reply_to: Optional[str] = None    # Message-ID this is a reply to
    references_header: Optional[str] = None  # Threading References header
    imap_uid: Optional[str] = None       # IMAP UID (prevents duplicate processing)
    source: Optional[str] = None         # provenance: 'system' (default) / 'zoho' (manual)
    sent_at: Optional[str] = None
    delivered_at: Optional[str] = None
    replied_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lead_id": self.lead_id,
            "customer_id": self.customer_id,
            "template_id": self.template_id,
            "channel": self.channel,
            "direction": self.direction,
            "subject": self.subject,
            "status": self.status,
            "error_message": self.error_message,
            "message_id": self.message_id,
            "in_reply_to": self.in_reply_to,
            "source": self.source,
            "sent_at": self.sent_at,
            "delivered_at": self.delivered_at,
            "replied_at": self.replied_at,
        }


@dataclass
class Campaign:
    """A batch outreach campaign (multiple leads, one template strategy)."""

    id: Optional[int] = None
    name: str = ""
    template_id: Optional[int] = None
    target_filter: Optional[str] = None  # JSON: {"country": "US", "industry": "fasteners"}
    status: str = "draft"                # 'draft', 'scheduled', 'running', 'completed'
    scheduled_at: Optional[str] = None
    completed_at: Optional[str] = None
    total_sent: int = 0
    total_bounced: int = 0
    total_replied: int = 0
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "template_id": self.template_id,
            "target_filter": self.target_filter,
            "status": self.status,
            "scheduled_at": self.scheduled_at,
            "completed_at": self.completed_at,
            "total_sent": self.total_sent,
            "total_bounced": self.total_bounced,
            "total_replied": self.total_replied,
        }


@dataclass
class BlacklistEntry:
    """Bounced or unsubscribed email — never send to these."""

    id: Optional[int] = None
    email: str = ""
    reason: str = ""                     # 'bounced', 'unsubscribed', 'complaint', 'invalid'
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "reason": self.reason,
        }


@dataclass
class SendingQuota:
    """Daily sending quota tracker (one row per date)."""

    id: Optional[int] = None
    date: str = ""                       # 'YYYY-MM-DD'
    email_count: int = 0
    domain_counts: str = "{}"            # JSON: {"gmail.com": 5, "yahoo.com": 2}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "date": self.date,
            "email_count": self.email_count,
            "domain_counts": self.domain_counts,
        }


@dataclass
class MailboxInfo:
    """A configured SMTP mailbox (env-backed pre-V8, DB-backed from M5).

    Passwords are NEVER serialized — only a has_smtp_password flag, so the
    API and UI can show "已配置/未配置" without ever exposing the secret.
    """

    name: str = ""                        # 'default' | 'zoho' | <user-added>
    display_name: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    use_tls: bool = True
    username: str = ""
    from_email: str = ""
    from_name: str = ""
    has_smtp_password: bool = False       # never the password itself
    daily_limit: int = 0
    sent_today: int = 0
    remaining: int = 0
    quota_table: str = ""                 # 'sending_quotas' | 'mailbox_quotas'
    is_active: int = 1
    source: str = "env"                   # 'env' pre-M5, 'db' post-M5
    credential_source: str = "missing"    # 'encrypted' | 'env' | 'missing' (OPT-005)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "use_tls": self.use_tls,
            "username": self.username,
            "from_email": self.from_email,
            "from_name": self.from_name,
            "has_smtp_password": self.has_smtp_password,
            "daily_limit": self.daily_limit,
            "sent_today": self.sent_today,
            "remaining": self.remaining,
            "quota_table": self.quota_table,
            "is_active": self.is_active,
            "source": self.source,
            "credential_source": self.credential_source,
        }


@dataclass
class TagInfo:
    """A lead tag. Read-only in M1; write path arrives with schema V7 (M4)."""

    id: Optional[int] = None
    name: str = ""
    category: str = "custom"              # 'custom' | 'auto' | 'source' | ...
    color: str = "#64748b"
    created_at: Optional[str] = None
    lead_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "color": self.color,
            "created_at": self.created_at,
            "lead_count": self.lead_count,
        }


@dataclass
class GroupInfo:
    """A lead group. Read-only in M1; write path arrives with schema V7 (M4)."""

    id: Optional[int] = None
    name: str = ""
    description: Optional[str] = None
    created_at: Optional[str] = None
    member_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "member_count": self.member_count,
        }
