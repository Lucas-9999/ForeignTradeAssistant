"""Dashboard router — GET /api/v1/dashboard/overview (read-only).

Query-heavy payload (KPIs + 4×30-day series + 4 distributions) is computed by
crm.dashboard_service directly against the live tables on a single connection.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from crm.db import get_db
from crm.dashboard_service import get_dashboard_details, get_dashboard_overview

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


@router.get("/overview")
async def dashboard_overview():
    """Dashboard header KPIs + 30-day series + distribution charts."""
    conn = None
    try:
        conn = get_db()
        return get_dashboard_overview(conn)
    except Exception as e:  # noqa: BLE001 — surface a clean 500 to the SPA
        raise HTTPException(status_code=500, detail=f"dashboard query failed: {e}")
    finally:
        if conn is not None:
            conn.close()


@router.get("/details/{metric}")
async def dashboard_details(
    metric: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
):
    """Paginated records behind a dashboard KPI card."""
    conn = get_db()
    try:
        try:
            return get_dashboard_details(
                conn, metric, page=page, page_size=page_size
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="仪表盘指标不存在") from exc
    finally:
        conn.close()
