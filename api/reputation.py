"""Read-only reputation snapshots for the CRM web console (P8).

This router deliberately exposes only P5 records already persisted in
``domain_health``.  It does not trigger DNS checks, score recomputation,
throttling, or sending actions.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from crm.db import get_db
from services.email_reputation.models import DomainHealth
from services.email_reputation.repository import EmailReputationRepository

router = APIRouter(prefix="/api/v1/reputation", tags=["reputation"])


def _domain_health_payload(health: DomainHealth) -> dict[str, object]:
    """Serialize the stable, non-sensitive P5 domain-health contract."""
    return {
        "domain": health.domain,
        "health_score": health.health_score,
        "status": health.status.value,
        "mx_status": health.mx_status.value,
        "spf_status": health.spf_status.value,
        "dkim_status": health.dkim_status.value,
        "dmarc_status": health.dmarc_status.value,
        "a_status": health.a_status.value,
        "ptr_status": health.ptr_status.value,
        "tls_status": health.tls_status.value,
        "sending_ip": health.sending_ip,
        "authentication_status": health.authentication_status.value,
        "last_checked": health.last_checked,
        "updated_at": health.updated_at,
    }


@router.get("/domain-health")
async def list_domain_health(limit: int = Query(default=50, ge=1, le=100)):
    """List P5 domain snapshots; this endpoint never starts a DNS audit."""
    conn = get_db()
    try:
        repository = EmailReputationRepository(conn)
        return {
            "source": "p5_persisted_snapshot",
            "domains": [
                _domain_health_payload(health)
                for health in repository.list_domain_health(limit=limit)
            ],
        }
    finally:
        conn.close()
