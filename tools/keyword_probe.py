"""
XunKeXing keyword probe — verify whether keyword-filtered searches surface
ANY data beyond a DEEP unfiltered scan, and whether English keywords match
anything in non-English countries.

Round 1 (2026-08-13) showed 1261 "NEW vs unfiltered" emails — but the unfiltered
baseline was capped at 300/country, so those were mostly truncation artifacts.
This round fixes that: baseline runs at the production depth (1000/country, 20
pages) AND compares against the emails already in the DB.

No DB writes — collects email sets and compares only.

Usage:
    python tools/keyword_probe.py
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

import config  # noqa: E402
from collector.sources.xunkexing import (  # noqa: E402
    XunKeXingCollector,
    FASTENER_KEYWORDS_TIER1,
)

COUNTRIES = ["united_states", "united_kingdom", "germany", "turkey"]
KEYWORDS = FASTENER_KEYWORDS_TIER1  # bolt, screw, nut, washer, fastener, rivet
DEEP_PER = 1000  # production depth (20 pages/country) for the unfiltered baseline


def emails_of(leads: list[dict]) -> set[str]:
    return {
        (l.get("email") or "").strip().lower()
        for l in leads
        if (l.get("email") or "").strip()
    }


def db_emails() -> set[str]:
    """All emails already in the leads table."""
    db_path = config.DB_PATH
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT email FROM leads WHERE email IS NOT NULL AND email != ''"
        ).fetchall()
    finally:
        conn.close()
    return {r[0].strip().lower() for r in rows}


async def collect_once(country_keys, keywords, include_unfiltered, max_per) -> tuple:
    col = XunKeXingCollector(
        country_keys=country_keys,
        keywords=keywords,
        include_unfiltered=include_unfiltered,
        headless=True,
        max_per_country=max_per,
    )
    leads = await col.collect()
    per_country = {
        ck: r.get("leads_found", 0) for ck, r in col._country_results.items()
    }
    return leads, per_country


async def main() -> None:
    print(f"=== XunKeXing keyword probe — round 2 (deep baseline) ===")
    print(f"countries : {COUNTRIES}")
    print(f"keywords  : {KEYWORDS}  ({len(KEYWORDS)})")
    print(f"deep per  : {DEEP_PER}/country for unfiltered baseline")
    print()

    existing = db_emails()
    print(f"DB already holds {len(existing)} emails")
    print()

    # ── Baseline: DEEP unfiltered (production depth) ───────────────────
    print("── [1/2] DEEP unfiltered baseline ─────────────────────────")
    base_leads, base_country = await collect_once(
        COUNTRIES, keywords=[], include_unfiltered=True, max_per=DEEP_PER
    )
    base_emails = emails_of(base_leads)
    print(f"deep unfiltered: {len(base_leads)} leads, {len(base_emails)} unique emails")
    for ck in COUNTRIES:
        print(f"  {ck:>16}: {base_country.get(ck, 0)}")
    print()

    # ── Per-keyword scans ───────────────────────────────────────────────
    print("── [2/2] Keyword scans ────────────────────────────────────")
    # kw → (leads, uniq, new_vs_deep, new_vs_db, per_country)
    rows: list[tuple[str, int, int, int, dict]] = []
    for kw in KEYWORDS:
        leads, per_country = await collect_once(
            COUNTRIES, keywords=[kw], include_unfiltered=False, max_per=DEEP_PER
        )
        kw_emails = emails_of(leads)
        new_vs_deep = len(kw_emails - base_emails)
        new_vs_db = len(kw_emails - existing)
        rows.append((kw, len(leads), new_vs_deep, new_vs_db, per_country))
        print(
            f"'{kw:>8}': {len(leads):>4} leads, {len(kw_emails):>4} uniq, "
            f"{new_vs_deep:>4} beyond deep-unfiltered, {new_vs_db:>4} beyond DB"
        )
        for ck in COUNTRIES:
            print(f"    {ck:>16}: {per_country.get(ck, 0)}")
        await asyncio.sleep(1.5)

    print()
    print("=== VERDICT ===")
    sum_deep = sum(r[2] for r in rows)
    sum_db = sum(r[3] for r in rows)
    print(f"Keywords total: {sum_deep} emails beyond DEEP unfiltered, "
          f"{sum_db} emails not yet in DB")
    if sum_deep == 0 and sum_db == 0:
        print("→ Keywords return only subsets of the deep unfiltered scan AND")
        print("  nothing new vs the DB. .env's 2026-08-08 note is CONFIRMED —")
        print("  enabling keyword search would waste ~8073 API queries.")
    elif sum_deep == 0:
        print("→ Keywords are subsets of deep unfiltered, BUT they still surface")
        print("  leads not yet in the DB (deep unfiltered itself would too).")
        print("  No reason to prefer keywords over unfiltered depth.")
    else:
        print(f"→ Keywords surfaced {sum_deep} emails a deep unfiltered scan misses.")
        print("  Keyword search may genuinely scale coverage — enable and watch first run.")


if __name__ == "__main__":
    asyncio.run(main())
