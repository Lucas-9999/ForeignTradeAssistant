"""
Health Monitor — tracks per-run collection metrics for risk analysis.

Tracks during each collection run:
  - Per-request: latency, status code, error type
  - Aggregated: avg/max latency, error counts, rate limits, circuit breaker events
  - Per-country: success/failure, leads found
  - Run-level: health score (0-100), overall success rate

The health score drops with:
  - High error rate (> 10% → deduct)
  - Rate limiting events
  - Circuit breaker openings
  - High latency (suggests server stress)

All metrics are persisted to the collection_health table for historical analysis.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class RequestMetric:
    """A single HTTP request measurement."""
    timestamp: float = field(default_factory=time.monotonic)
    latency_ms: float = 0.0
    status_code: int = 0
    error_type: Optional[str] = None  # 'timeout', 'connection', 'http_5xx', 'http_429', etc.
    country_key: Optional[str] = None


class HealthMonitor:
    """Tracks per-run health metrics for a collection session.

    Usage:
        monitor = HealthMonitor(source="xunkexing")
        monitor.start_run(countries_total=6)

        # ... during collection, for each request:
        monitor.record_request(latency_ms=234.5, status_code=200)
        monitor.record_request(latency_ms=0, status_code=0, error_type="timeout")

        # Per country:
        monitor.record_country_result("germany", success=True, leads_found=15)

        # End of run:
        monitor.end_run(leads_found=80, leads_new=12)
        monitor.save_to_db(conn, task_id=123)
    """

    def __init__(self, source: str = "unknown"):
        self.source = source
        self._requests: list[RequestMetric] = []
        self._countries_total: int = 0
        self._countries_collected: int = 0
        self._countries_failed: int = 0
        self._leads_found: int = 0
        self._leads_new: int = 0
        self._circuit_breaker_hits: int = 0
        self._started_at: Optional[str] = None
        self._completed_at: Optional[str] = None
        self._country_details: dict[str, dict] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start_run(self, countries_total: int = 0):
        """Mark the start of a collection run."""
        self._started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._countries_total = countries_total
        self._requests.clear()
        self._country_details.clear()
        logger.info(
            f"[{self.source}] Health monitor started — "
            f"targeting {countries_total} countries"
        )

    def end_run(self, leads_found: int = 0, leads_new: int = 0, error: Optional[str] = None):
        """Mark the end of a collection run."""
        self._completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._leads_found = leads_found
        self._leads_new = leads_new
        if error:
            logger.warning(f"[{self.source}] Run ended with error: {error}")

        health = self.compute_health_score()
        logger.info(
            f"[{self.source}] Health monitor finished — "
            f"score={health:.0f}/100, "
            f"requests={self.total_requests}, "
            f"errors={self.error_count}, "
            f"rate_limits={self.rate_limit_count}"
        )

    # ── Recording ──────────────────────────────────────────────────────────

    def record_request(
        self,
        latency_ms: float = 0.0,
        status_code: int = 0,
        error_type: Optional[str] = None,
        country_key: Optional[str] = None,
    ):
        """Record a single HTTP request outcome."""
        self._requests.append(RequestMetric(
            latency_ms=latency_ms,
            status_code=status_code,
            error_type=error_type,
            country_key=country_key,
        ))

    def record_country_result(
        self,
        country_key: str,
        success: bool,
        leads_found: int = 0,
    ):
        """Record the result of collecting one country."""
        self._country_details[country_key] = {
            "success": success,
            "leads_found": leads_found,
        }
        if success:
            self._countries_collected += 1
        else:
            self._countries_failed += 1

    def record_circuit_breaker(self):
        """Record a circuit breaker opening event."""
        self._circuit_breaker_hits += 1
        logger.warning(
            f"[{self.source}] Circuit breaker event recorded "
            f"(#{self._circuit_breaker_hits} this run)"
        )

    # ── Computed metrics ───────────────────────────────────────────────────

    @property
    def total_requests(self) -> int:
        return len(self._requests)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self._requests if r.error_type is not None or r.status_code >= 400)

    @property
    def rate_limit_count(self) -> int:
        return sum(1 for r in self._requests if r.error_type == "http_429" or r.status_code == 429)

    @property
    def avg_latency_ms(self) -> float:
        successful = [r for r in self._requests if r.latency_ms > 0]
        if not successful:
            return 0.0
        return sum(r.latency_ms for r in successful) / len(successful)

    @property
    def max_latency_ms(self) -> float:
        latencies = [r.latency_ms for r in self._requests if r.latency_ms > 0]
        return max(latencies) if latencies else 0.0

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return (self.total_requests - self.error_count) / self.total_requests

    def compute_health_score(self) -> float:
        """Compute a health score from 0 (worst) to 100 (best).

        Deductions:
          - Error rate > 0%: up to -40 points (proportional)
          - Rate limit events: -10 per event (max -30)
          - Circuit breaker hits: -20 per event (max -40)
          - High avg latency (> 5s): up to -20 points
          - Countries failed > 50%: -15 points
        """
        score = 100.0

        # Error rate deduction (up to 40 points)
        if self.total_requests > 0:
            error_rate = self.error_count / self.total_requests
            score -= min(40.0, error_rate * 80.0)  # 50% error rate = -40

        # Rate limit deduction (10 per event, max 30)
        score -= min(30.0, self.rate_limit_count * 10.0)

        # Circuit breaker deduction (20 per hit, max 40)
        score -= min(40.0, self._circuit_breaker_hits * 20.0)

        # Latency deduction (if avg > 5s, up to 20 points)
        if self.avg_latency_ms > 5000:
            excess = (self.avg_latency_ms - 5000) / 1000  # per second over 5s
            score -= min(20.0, excess * 2.0)

        # Country failure deduction
        if self._countries_total > 0:
            fail_rate = self._countries_failed / self._countries_total
            if fail_rate > 0.5:
                score -= 15.0

        return max(0.0, score)

    # ── Persistence ────────────────────────────────────────────────────────

    def save_to_db(self, conn: sqlite3.Connection, task_id: Optional[int] = None):
        """Persist this run's metrics to the collection_health table."""
        health_score = self.compute_health_score()

        details = json.dumps({
            "per_country": self._country_details,
            "error_distribution": self._error_distribution(),
        }, ensure_ascii=False)

        conn.execute(
            """INSERT INTO collection_health
               (task_id, source, started_at, completed_at,
                avg_latency_ms, max_latency_ms, total_requests,
                error_count, rate_limit_count, circuit_breaker_hits,
                countries_total, countries_collected, countries_failed,
                leads_found, leads_new, health_score, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                self.source,
                self._started_at,
                self._completed_at,
                round(self.avg_latency_ms, 1),
                round(self.max_latency_ms, 1),
                self.total_requests,
                self.error_count,
                self.rate_limit_count,
                self._circuit_breaker_hits,
                self._countries_total,
                self._countries_collected,
                self._countries_failed,
                self._leads_found,
                self._leads_new,
                round(health_score, 1),
                details,
            ),
        )
        conn.commit()
        logger.info(
            f"[{self.source}] Health saved to DB — "
            f"score={health_score:.0f}/100"
        )

    def _error_distribution(self) -> dict[str, int]:
        """Count errors by type."""
        dist: dict[str, int] = {}
        for r in self._requests:
            if r.error_type:
                dist[r.error_type] = dist.get(r.error_type, 0) + 1
            elif r.status_code >= 400:
                key = f"http_{r.status_code}"
                dist[key] = dist.get(key, 0) + 1
        return dist

    # ── Summary ────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Get a summary dict for API responses."""
        return {
            "source": self.source,
            "started_at": self._started_at,
            "completed_at": self._completed_at,
            "total_requests": self.total_requests,
            "error_count": self.error_count,
            "rate_limit_count": self.rate_limit_count,
            "circuit_breaker_hits": self._circuit_breaker_hits,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "max_latency_ms": round(self.max_latency_ms, 1),
            "success_rate": round(self.success_rate * 100, 1),
            "health_score": round(self.compute_health_score(), 1),
            "countries_total": self._countries_total,
            "countries_collected": self._countries_collected,
            "countries_failed": self._countries_failed,
            "leads_found": self._leads_found,
            "leads_new": self._leads_new,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Historical queries (for dashboard)
# ═══════════════════════════════════════════════════════════════════════════


def get_health_history(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
    days: int = 7,
    limit: int = 50,
) -> list[dict]:
    """Get health records for recent runs."""
    where = "WHERE started_at >= datetime('now', ?)"
    params: list = [f"-{days} days"]
    if source:
        where += " AND source = ?"
        params.append(source)

    rows = conn.execute(
        f"""SELECT * FROM collection_health
            {where}
            ORDER BY started_at DESC
            LIMIT ?""",
        (*params, limit),
    ).fetchall()

    return [dict(r) for r in rows]


def get_health_trend(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
    days: int = 7,
) -> list[dict]:
    """Get daily health score averages for trend chart."""
    where = "WHERE started_at >= datetime('now', ?)"
    params: list = [f"-{days} days"]
    if source:
        where += " AND source = ?"
        params.append(source)

    rows = conn.execute(
        f"""SELECT date(started_at) as day,
                   ROUND(AVG(health_score), 1) as avg_score,
                   SUM(total_requests) as requests,
                   SUM(error_count) as errors,
                   SUM(leads_new) as leads,
                   COUNT(*) as runs
            FROM collection_health
            {where}
            GROUP BY day
            ORDER BY day ASC""",
        params,
    ).fetchall()

    return [dict(r) for r in rows]


def get_latest_health(conn: sqlite3.Connection, source: str = "xunkexing") -> Optional[dict]:
    """Get the most recent health record for a source."""
    row = conn.execute(
        """SELECT * FROM collection_health
           WHERE source = ?
           ORDER BY started_at DESC
           LIMIT 1""",
        (source,),
    ).fetchone()
    return dict(row) if row else None
