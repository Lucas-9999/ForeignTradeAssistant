"""
Collector CLI — command-line entry point for running data collection.

Usage:
    python -m collector run --source=industry
    python -m collector run --source=xunkexing --dry-run
    python -m collector run --all
    python -m collector list-sources
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path (needed for `python -m collector` invocation)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from crm.db import build_database, get_connection
from crm.manager import LeadManager
from collector.pipeline import ingest_leads
from collector.monitor import HealthMonitor
import config

logger = logging.getLogger("collector.cli")


# ═══════════════════════════════════════════════════════════════════════════
# Source Registry
# ═══════════════════════════════════════════════════════════════════════════
#
# Each source entry maps a source name to a factory function that returns
# a BaseCollector instance. Add new sources here as they are built.

def build_source_registry(settings=config) -> dict[str, callable]:
    """Build the currently enabled collection source registry.

    Paused sources are not merely hidden in the UI: they have no CLI factory,
    so ``collector run --all`` cannot accidentally start them.
    """
    registry: dict[str, callable] = {}

    if getattr(settings, "COLLECTION_INDUSTRY_ENABLED", True):
        try:
            from collector.sources.industry import IndustryCollector
            registry["industry"] = lambda: IndustryCollector()
        except ImportError:
            pass

    if getattr(settings, "COLLECTION_XUNKEXING_ENABLED", False):
        try:
            from collector.sources.xunkexing import XunKeXingCollector

            def _xkx_factory():
                use_all = getattr(settings, "COLLECTION_USE_ALL_COUNTRIES", False)
                use_kw = getattr(settings, "COLLECTION_USE_KEYWORD_SEARCH", False)
                max_per = getattr(settings, "COLLECTION_MAX_PER_COUNTRY", 200)
                return XunKeXingCollector(
                    headless=True,
                    use_all_countries=use_all,
                    use_keyword_search=use_kw,
                    max_per_country=max_per,
                )

            registry["xunkexing"] = _xkx_factory
        except ImportError:
            pass

    if getattr(settings, "FB_COLLECTION_ENABLED", False):
        try:
            from collector.sources.facebook import FacebookCollector
            registry["facebook"] = lambda: FacebookCollector()
        except ImportError:
            pass

    if getattr(settings, "COLLECTION_LINKEDIN_ENABLED", False):
        try:
            from collector.sources.linkedin import LinkedInCollector
            registry["linkedin"] = lambda: LinkedInCollector(headless=True)
        except ImportError:
            pass

    return registry


SOURCE_REGISTRY = build_source_registry()


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def run_collection(
    sources: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, dict]:
    """Run collection for specified sources (or all registered).

    Integrates HealthMonitor and SegmentManager so that:
    - Health metrics (latency, errors, rate limits) are persisted to DB
    - Country rotation state is updated after each collection
    - Dashboard shows real data after CLI-run collections

    Returns {source_name: result_dict} mapping (see ingest_leads for format).
    """
    if sources is None:
        sources = list(SOURCE_REGISTRY.keys())

    if not sources:
        logger.error("No sources registered. Build source modules first.")
        return {}

    # Ensure database exists
    db_path = config.DB_PATH
    if not db_path.exists():
        logger.info(f"Creating database at {db_path}")
        build_database(db_path)

    conn = get_connection(db_path)
    lead_mgr = LeadManager(conn)

    # Segment manager for country rotation tracking
    from collector.segments import SegmentManager
    segment_mgr = SegmentManager(conn)

    all_results: dict[str, dict] = {}

    for source_name in sources:
        factory = SOURCE_REGISTRY.get(source_name)
        if factory is None:
            logger.warning(f"Unknown source: '{source_name}'. Skipping.")
            continue

        logger.info(f"{'='*60}")
        logger.info(f"Running collector: {source_name}")
        logger.info(f"{'='*60}")

        # ── Set up health monitor for this source ───────────────────────
        monitor = HealthMonitor(source=source_name)

        collector = None
        try:
            collector = factory()
            monitor.start_run()

            raw_leads = await collector.collect()
            logger.info(f"[{source_name}] Collected {len(raw_leads)} raw leads")

            # Record rate limiter stats if available
            rl = getattr(collector, '_rate_limiter', None)
            if rl is not None:
                rl_stats = rl.get_stats()
                if rl_stats.get("circuit_open"):
                    monitor.record_circuit_breaker()
                # Pull request/error counts from rate limiter
                for _ in range(rl_stats.get("total_requests", 0)):
                    monitor.record_request(latency_ms=0, status_code=200)
                for _ in range(rl_stats.get("total_errors", 0)):
                    monitor.record_request(latency_ms=0, status_code=0, error_type="rate_limiter_error")

            # ── Per-country tracking (XunKeXing-specific) ──────────────
            country_results = getattr(collector, '_country_results', {})
            if country_results:
                monitor._countries_total = len(country_results)
                for ck, cr in country_results.items():
                    success = cr.get("success", False)
                    c_leads = cr.get("leads_found", 0)
                    monitor.record_country_result(ck, success=success, leads_found=c_leads)
                    # Update segment rotation state
                    segment_mgr.record_collection(
                        ck,
                        leads_found=c_leads,
                        had_error=not success,
                    )

            # ── Ingest leads ───────────────────────────────────────────
            result = ingest_leads(raw_leads, source_name, lead_mgr, dry_run=dry_run)
            all_results[source_name] = result

            # Finish monitoring
            leads_found = len(raw_leads)
            leads_new = result.get("created", 0)
            monitor.end_run(leads_found=leads_found, leads_new=leads_new)

            # Persist health metrics
            if not dry_run:
                monitor.save_to_db(conn)

            # Log scraping task
            if not dry_run:
                _log_task(
                    source_name, "completed",
                    leads_found=leads_found, leads_new=leads_new,
                    health_score=monitor.compute_health_score(),
                )

            health = monitor.compute_health_score()
            logger.info(
                f"[{source_name}] Health: {health:.0f}/100 | "
                f"Leads: {leads_found} raw → {leads_new} new | "
                f"Countries: {monitor._countries_collected}/{monitor._countries_total}"
            )

        except Exception:
            logger.exception(f"[{source_name}] Collection failed with error")
            monitor.end_run(leads_found=0, leads_new=0, error="Collection failed")
            if not dry_run:
                monitor.save_to_db(conn)
                _log_task(source_name, "failed", leads_found=0, leads_new=0,
                          health_score=monitor.compute_health_score())

            all_results[source_name] = {
                "total_raw": 0,
                "valid": 0,
                "skipped_invalid": 0,
                "created": 0,
                "duplicates_skipped": 0,
                "dry_run": dry_run,
                "error": True,
            }
        finally:
            if collector:
                await collector.close()

    conn.close()

    # ── Summary ────────────────────────────────────────────────────────
    total_raw = sum(r.get("total_raw", 0) for r in all_results.values())
    total_created = sum(r.get("created", 0) for r in all_results.values())
    logger.info(
        f"\n{'='*60}\n"
        f"Collection complete: {total_raw} raw → {total_created} new leads "
        f"across {len(all_results)} source(s)\n"
        f"{'='*60}"
    )

    return all_results


def _log_task(
    source: str,
    status: str,
    leads_found: int = 0,
    leads_new: int = 0,
    health_score: float = 0,
    error: str = "",
):
    """Log a collection task to the scraping_tasks table."""
    import sqlite3
    from datetime import datetime, timezone

    db_path = config.DB_PATH
    if not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        error_msg = error or (f"Health: {health_score:.0f}/100")
        conn.execute(
            """INSERT INTO scraping_tasks (source, status, leads_found, leads_new,
               error_message, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source, status, leads_found, leads_new, error_msg, now, now),
        )
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m collector",
        description="FTA Information Collection Module — 信息采集模块",
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # ── run ────────────────────────────────────────────────────────────
    run_parser = subparsers.add_parser("run", help="Run data collection")
    run_parser.add_argument(
        "--source", "-s",
        help="Source to run (e.g. industry, xunkexing, facebook)",
    )
    run_parser.add_argument(
        "--all", "-a",
        action="store_true",
        help="Run all registered sources",
    )
    run_parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Validate and preview without writing to DB",
    )

    # ── list-sources ───────────────────────────────────────────────────
    subparsers.add_parser("list-sources", help="List available data sources")

    return parser


def main():
    """CLI entry point."""
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=config.LOG_FORMAT,
    )

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list-sources":
        if SOURCE_REGISTRY:
            print("Available sources:")
            for name in SOURCE_REGISTRY:
                print(f"  - {name}")
        else:
            print("No sources registered yet.")
        return

    if args.command == "run":
        # Determine which sources to run
        if args.all:
            sources = None  # run all
        elif args.source:
            sources = [args.source]
        else:
            parser.error("Must specify --source or --all")

        results = asyncio.run(run_collection(sources=sources, dry_run=args.dry_run))

        # Print summary table
        print("\n" + "=" * 70)
        print(f"{'Source':<15} {'Raw':>6} {'Valid':>6} {'Created':>8} {'DupSkip':>8}")
        print("-" * 70)
        for src, r in sorted(results.items()):
            err = " ❌ ERROR" if r.get("error") else ""
            print(
                f"{src:<15} "
                f"{r.get('total_raw', 0):>6} "
                f"{r.get('valid', 0):>6} "
                f"{r.get('created', 0):>8} "
                f"{r.get('duplicates_skipped', 0):>8}"
                f"{err}"
            )
        print("=" * 70)
        total = sum(r.get("created", 0) for r in results.values())
        if total > 0:
            print(f"[OK] {total} new leads added to CRM")
        elif any(r.get("dry_run") for r in results.values()):
            print("[DRY RUN] Dry run — no data written")
        else:
            print("[INFO] No new leads found")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
