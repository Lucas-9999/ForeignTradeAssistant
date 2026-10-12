"""
Country Rotation Segment Engine — distributes collection across days.

Instead of hitting all 24 countries every day (risky pattern), this engine
selects a daily "segment" of countries to collect. Each day, a random subset
is chosen, with priority given to least-recently-collected countries.

Algorithm:
  1. Load rotation state from DB (last_collected_at per country)
  2. Sort by priority: never-collected > stale (> rotation_period days) > random
  3. Pick segment_size countries
  4. After collection, update last_collected_at timestamps

This spreads API load across days and makes traffic patterns less predictable.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import config

logger = logging.getLogger(__name__)


class SegmentManager:
    """Manages country rotation for segmented daily collection.

    Usage:
        mgr = SegmentManager(db_conn)
        today = mgr.select_today_countries(all_countries, segment_size=6)
        # ... collect data for today's countries ...
        for country in today:
            mgr.record_collection(country, leads_found=N)
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._ensure_table()

    def _ensure_table(self):
        """Ensure the segments table exists."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS collection_segments (
                country_key TEXT PRIMARY KEY,
                last_collected_at TIMESTAMP,
                collection_count INTEGER DEFAULT 0,
                last_leads_found INTEGER DEFAULT 0,
                consecutive_errors INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            )
        """)
        self._conn.commit()

    # ── Country selection ─────────────────────────────────────────────────

    def select_today_countries(
        self,
        all_countries: list[str],
        segment_size: Optional[int] = None,
    ) -> list[str]:
        """Select which countries to collect today.

        Priority:
        1. Countries never collected (highest priority)
        2. Countries not collected in the last rotation_period days
        3. Random selection from remaining

        Args:
            all_countries: Complete list of available country keys.
            segment_size: How many to pick today (default: config.COLLECTION_SEGMENT_SIZE).

        Returns:
            Ordered list of country keys to collect today.
        """
        segment_size = segment_size or config.COLLECTION_SEGMENT_SIZE
        segment_size = min(segment_size, len(all_countries))

        # Load current state
        state = self._load_state(all_countries)

        # Calculate rotation period: how many days to cover all countries
        rotation_period_days = max(3, len(all_countries) // segment_size)

        # Use naive datetime for consistency with SQLite TEXT storage
        now_naive = datetime.now().replace(microsecond=0)

        # Score each country (lower = higher priority)
        scored: list[tuple[str, int]] = []
        for ck in all_countries:
            entry = state.get(ck)
            if entry is None:
                # Never collected — highest priority
                scored.append((ck, -1000))
            elif entry["last_collected_at"] is None:
                scored.append((ck, -999))
            else:
                try:
                    last_dt = datetime.fromisoformat(entry["last_collected_at"])
                    if last_dt.tzinfo is not None:
                        last_dt = last_dt.replace(tzinfo=None)
                    days_ago = (now_naive - last_dt).days
                except (ValueError, TypeError):
                    days_ago = 999

                # Score: days_ago (higher = more due) minus random factor
                # Countries past rotation period get a bonus
                priority = -days_ago
                if days_ago >= rotation_period_days:
                    priority -= 500  # boost stale countries
                # Add small random factor to prevent deterministic ordering
                priority += random.randint(0, 100)
                scored.append((ck, priority))

        # Sort by priority (lowest first = most due)
        scored.sort(key=lambda x: x[1])

        # Pick top segment_size
        selected = [ck for ck, _ in scored[:segment_size]]

        # Shuffle to avoid always collecting in same order
        random.shuffle(selected)

        logger.info(
            f"Segment: {len(selected)}/{len(all_countries)} countries selected. "
            f"Rotation period: ~{rotation_period_days} days. "
            f"Selected: {selected}"
        )

        return selected

    def _load_state(self, all_countries: list[str]) -> dict[str, dict]:
        """Load rotation state from DB, initializing entries for new countries."""
        rows = self._conn.execute(
            "SELECT country_key, last_collected_at, collection_count, "
            "last_leads_found, consecutive_errors, is_active "
            "FROM collection_segments"
        ).fetchall()

        state = {}
        for row in rows:
            state[row["country_key"]] = {
                "last_collected_at": row["last_collected_at"],
                "collection_count": row["collection_count"],
                "last_leads_found": row["last_leads_found"],
                "consecutive_errors": row["consecutive_errors"],
                "is_active": row["is_active"],
            }

        # Initialize missing countries
        for ck in all_countries:
            if ck not in state:
                state[ck] = {
                    "last_collected_at": None,
                    "collection_count": 0,
                    "last_leads_found": 0,
                    "consecutive_errors": 0,
                    "is_active": 1,
                }

        return state

    # ── Record results ─────────────────────────────────────────────────────

    def record_collection(
        self,
        country_key: str,
        leads_found: int = 0,
        had_error: bool = False,
    ):
        """Update rotation state after collecting a country."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if had_error:
            self._conn.execute(
                """INSERT INTO collection_segments
                   (country_key, last_collected_at, consecutive_errors)
                   VALUES (?, ?, 1)
                   ON CONFLICT(country_key) DO UPDATE SET
                   last_collected_at = excluded.last_collected_at,
                   consecutive_errors = consecutive_errors + 1""",
                (country_key, now),
            )
        else:
            self._conn.execute(
                """INSERT INTO collection_segments
                   (country_key, last_collected_at, collection_count,
                    last_leads_found, consecutive_errors)
                   VALUES (?, ?, 1, ?, 0)
                   ON CONFLICT(country_key) DO UPDATE SET
                   last_collected_at = excluded.last_collected_at,
                   collection_count = collection_count + 1,
                   last_leads_found = excluded.last_leads_found,
                   consecutive_errors = 0""",
                (country_key, now, leads_found),
            )
        self._conn.commit()

    # ── Query ──────────────────────────────────────────────────────────────

    def get_rotation_status(self) -> list[dict]:
        """Get full rotation status for all countries."""
        rows = self._conn.execute(
            """SELECT country_key, last_collected_at, collection_count,
                      last_leads_found, consecutive_errors, is_active
               FROM collection_segments
               ORDER BY last_collected_at ASC NULLS FIRST"""
        ).fetchall()

        return [
            {
                "country_key": row["country_key"],
                "last_collected_at": row["last_collected_at"],
                "collection_count": row["collection_count"],
                "last_leads_found": row["last_leads_found"],
                "consecutive_errors": row["consecutive_errors"],
                "is_active": bool(row["is_active"]),
            }
            for row in rows
        ]

    def get_next_prediction(
        self, all_countries: list[str], segment_size: int = 6
    ) -> list[str]:
        """Predict which countries would be selected next (without recording)."""
        return self.select_today_countries(all_countries, segment_size)
