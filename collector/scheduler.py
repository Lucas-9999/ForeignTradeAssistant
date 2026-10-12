"""
Collection Scheduler — randomized daily execution with segmented collection.

Key improvements over the original:
1. Random time: picks a random minute within [window_start, window_end] each day
2. Segmented collection: only collects N countries per day via SegmentManager
3. Health monitoring: tracks latency, errors, rate limits per run
4. Auto-reschedule: after each run, picks tomorrow's random time

Integration with FastAPI (app.py):
    from collector.scheduler import start_scheduler
    start_scheduler()  # called on app startup
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import random
import sqlite3
import threading
from datetime import date, datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config

logger = logging.getLogger("collector.scheduler")

# A task with status='running' older than this is presumed stale (the process
# that started it was killed before it completed). A real full keyword scan
# takes up to ~5h, so 6h is a safe ceiling.
STALE_RUNNING_HOURS = 6

# Scheduler singleton
_scheduler: Optional[BackgroundScheduler] = None
_current_job_id = "daily_collection"


def scheduler_is_running() -> bool:
    """Whether this process has an active scheduler for P13 monitor gating."""
    return bool(_scheduler is not None and _scheduler.running)

# Track next run time for display
_next_run_info: dict = {"hour": None, "minute": None, "scheduled_at": None}

# 进程内互斥锁（belt-and-suspenders）：DB 层 batch_run_guards 的原子 claim
# 已是跨进程/跨入口的最终防线，这把锁让同一进程内两个触发线程的 claim
# 顺序可见、跳过日志更清晰。
_EMAIL_BATCH_LOCK = threading.Lock()
_ZOHO_BATCH_LOCK = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════
# Batch Run Mutual Exclusion (V9) — 每日每 kind 至多一个活跃批次
# ═══════════════════════════════════════════════════════════════════════════


def _claim_batch_run(kind: str, date_str: str, stale_hours: float = 3.0) -> bool:
    """原子地 claim 今天的 `kind` 批次。

    返回 True = 本进程拿到本次执行权；False = 今天该 kind 已有批次在跑
    或已跑完（其余触发入口应记日志跳过）。

    DELETE 与 INSERT 在同一写事务内完成，四种情形一把全收：
      - 无记录        → DELETE 0 行，INSERT 成功 → True
      - 卡死残留      → DELETE 删除 >stale_hours 的 'running'，INSERT 成功 → True
      - 刚启动的 running → DELETE 0 行，INSERT OR IGNORE 撞唯一键 → False
      - 当天已 done   → DELETE 0 行，INSERT OR IGNORE 撞唯一键 → False
    """
    db_path = config.DB_PATH
    if not db_path.exists():
        # 无库可守——send_batch 内部仍会自行兜底（无库即无法发送）。
        return True
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        # ① 回收崩溃残留：超过 stale_hours 仍未 finish 的 'running' 视为卡死，
        #    删除腾位（DELETE 与 INSERT 在同一写事务内，原子）。
        conn.execute(
            "DELETE FROM batch_run_guards "
            "WHERE kind=? AND date=? AND status='running' "
            "AND (julianday('now') - julianday(started_at)) * 24.0 > ?",
            (kind, date_str, stale_hours),
        )
        # ② 原子 claim：INSERT OR IGNORE，主键 (kind, date) 唯一。
        cur = conn.execute(
            "INSERT OR IGNORE INTO batch_run_guards (kind, date, status) "
            "VALUES (?, ?, 'running')",
            (kind, date_str),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _finish_batch_run(kind: str, date_str: str) -> None:
    """把今天的 `kind` 批次标记为 'done'（best-effort，失败不阻断）。"""
    db_path = config.DB_PATH
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute(
            "UPDATE batch_run_guards SET status='done', "
            "finished_at=CURRENT_TIMESTAMP WHERE kind=? AND date=?",
            (kind, date_str),
        )
        conn.commit()
    except Exception:
        logger.exception("Failed to record batch run completion (kind=%s)", kind)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Time Randomization
# ═══════════════════════════════════════════════════════════════════════════


def _pick_random_minute(min_start: int = 1) -> int:
    """Pick a random minute avoiding :00 and :30 to reduce fleet bunching."""
    minute = random.randint(min_start, 59)
    while minute in (0, 30):
        minute = random.randint(min_start, 59)
    return minute


def _is_weekday(now=None) -> bool:
    """True if Mon-Fri. 自动批量发信仅工作日运行; 手动 UI/API/CLI 发送直接
    调 emailer.batch, 不经过调度器, 不受此限制."""
    now = now or datetime.now()
    return now.isoweekday() <= 5


def _email_schedule_start_date() -> Optional[date]:
    """Return the operator's optional batch start date, rejecting bad input."""
    raw = config.EMAIL_SCHEDULE_START_DATE
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.error("Email batch scheduler disabled: invalid FTA_EMAIL_SCHEDULE_START_DATE")
        return None


def _email_schedule_is_active(now: Optional[datetime] = None) -> bool:
    """Whether a configured email batch is allowed to run on this date."""
    raw = config.EMAIL_SCHEDULE_START_DATE
    start_date = _email_schedule_start_date()
    if raw and start_date is None:
        return False
    return start_date is None or (now or datetime.now()).date() >= start_date


def _pick_time_for_hour(hour: int) -> tuple[int, int]:
    """Pick (hour, random_minute) for a fixed-hour schedule slot."""
    return hour, _pick_random_minute()


def _pick_random_time() -> tuple[int, int]:
    """Pick a random hour:minute within the configured time window.

    Minute is randomized to avoid :00/:30 patterns.
    Respects SCHEDULE_TIME_WINDOW_START_MINUTE.
    """
    start = config.SCHEDULE_TIME_WINDOW_START
    start_min = getattr(config, 'SCHEDULE_TIME_WINDOW_START_MINUTE', 0)
    end = config.SCHEDULE_TIME_WINDOW_END

    hour = random.randint(start, max(start, end - 1))
    min_minute = start_min if hour == start else 1
    minute = _pick_random_minute(min_minute)

    logger.info(f"Randomized next collection: {hour:02d}:{minute:02d}")
    return hour, minute


def _format_next_run(hour: int, minute: int) -> str:
    """Format the next run time for display."""
    now = datetime.now()
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        # Already past today's window — will run tomorrow
        from datetime import timedelta
        next_run = next_run + timedelta(days=1)
    return next_run.strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════════════
# Scraping Task Log (DB)
# ═══════════════════════════════════════════════════════════════════════════


def _log_task_start(source: str) -> int:
    """Create a scraping_tasks row (status=running). Returns task id."""
    db_path = config.DB_PATH
    if not db_path.exists():
        from crm.db import build_database
        build_database(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            """INSERT INTO scraping_tasks (source, status, started_at, pid)
               VALUES (?, 'running', ?, ?)""",
            (
                source,
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                os.getpid(),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _log_task_complete(
    task_id: int,
    leads_found: int,
    leads_new: int,
    error: Optional[str] = None,
    countries_collected: int = 0,
    countries_failed: int = 0,
):
    """Update a scraping_tasks row to completed/failed with segment info."""
    db_path = config.DB_PATH
    conn = sqlite3.connect(str(db_path))
    try:
        status = "completed" if error is None else "failed"
        # Build error message with segment info
        full_error = error
        if countries_failed > 0:
            seg_info = f"[{countries_collected} OK, {countries_failed} failed]"
            full_error = f"{seg_info} {error}" if error else seg_info

        conn.execute(
            """UPDATE scraping_tasks
               SET status=?, leads_found=?, leads_new=?, error_message=?,
                   completed_at=?
               WHERE id=?""",
            (
                status,
                leads_found,
                leads_new,
                full_error,
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_own_running_tasks_failed(reason: str) -> int:
    """Mark still-running scraping_tasks owned by THIS process as failed.

    Called on graceful shutdown / interpreter exit. Without this, a collection
    killed by an app restart leaves a phantom 'running' row in scraping_tasks;
    the concurrency guard treats any recent running task as "a collection is in
    progress" and blocks the whole day's collection for up to STALE_RUNNING_HOURS
    (observed 2026-08-10: the 08:24 deep-scan was killed at 08:44 by a restart,
    task id=150 stayed 'running', and every subsequent catch-up skipped until
    the age-based stale cleanup would have kicked in).

    PID-scoped so a concurrent manual CLI collection (different process, own
    task row) is never clobbered by the app's exit cleanup.

    Returns the number of tasks marked failed.
    """
    db_path = config.DB_PATH
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            note = f" [interrupted: {reason}]"
            cur = conn.execute(
                """UPDATE scraping_tasks
                   SET status='failed', completed_at=datetime('now'),
                       error_message=COALESCE(error_message, '') || ?
                   WHERE status='running' AND pid = ?""",
                (note, os.getpid()),
            )
            n = cur.rowcount
            conn.commit()
            if n:
                logger.warning(
                    "Marked %d running task(s) as failed on %s", n, reason
                )
            return n
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to mark running tasks failed on %s", reason)
        return 0


# If the process exits without going through stop_scheduler() (e.g. uvicorn
# shutdown, Streamlit exit, unhandled exception), still mark OUR still-running
# crawl tasks as failed so they can't block future collections. Idempotent and
# PID-scoped, so it's safe to run even when no collection is in flight.
atexit.register(_mark_own_running_tasks_failed, "process exit")


def get_recent_tasks(days: int = 7, limit: int = 20) -> list[dict]:
    """Get recent scraping task history."""
    db_path = config.DB_PATH
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT * FROM scraping_tasks
               WHERE started_at >= datetime('now', ?)
               ORDER BY started_at DESC
               LIMIT ?""",
            (f"-{days} days", limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled Collection Job
# ═══════════════════════════════════════════════════════════════════════════


async def _collect_and_close(collector: "XunKeXingCollector") -> list[dict]:
    """Collect leads and close the collector in a single async context.

    Must be called via asyncio.run() ONCE — never split across two
    asyncio.run() calls, because the second call creates a new loop
    that crashes on Windows ProactorEventLoop when pending transport
    close-callbacks fire during shutdown.
    """
    try:
        return await collector.collect()
    finally:
        await collector.close()


def _run_scheduled_collection():
    """Execute a segmented, health-monitored collection cycle.

    This is the main scheduled job. It:
    1. Creates a health monitor for this run
    2. Uses SegmentManager to pick today's countries
    3. Runs the XunKeXing collector on that subset
    4. Records per-country results and health metrics
    5. Saves everything to DB
    6. Triggers the daily email batch (chained, non-blocking)
    7. Reschedules for a random time tomorrow
    """
    if not config.COLLECTION_XUNKEXING_ENABLED:
        logger.info(
            "XunKeXing collection is paused "
            "(FTA_XUNKEXING_COLLECTION_ENABLED=false)"
        )
        return

    logger.info("=" * 60)
    logger.info("SCHEDULED COLLECTION STARTING")
    logger.info("=" * 60)

    # ── Concurrency guard: skip if another collection is already running ──
    # The catch-up path (start_scheduler) has this guard, but the scheduled
    # daily job did not. When the app restarts inside the collection window
    # [08:00–10:00], the catch-up fires a collection (thread) AND the
    # scheduled job later fires again → two concurrent xunkexing crawls,
    # doubling API quota (observed 2026-08-05: tasks 142 + 143 in parallel).
    # The email batch has its own scheduled job + catch-up, so skipping here
    # does not starve it; the active crawl's finally block reschedules tomorrow.
    try:
        db_path = config.DB_PATH
        if db_path.exists():
            _conn = sqlite3.connect(str(db_path))
            try:
                # Only treat RECENT running tasks as "in progress". A task older
                # than STALE_RUNNING_HOURS is a phantom left by a killed process
                # (the startup stale-cleanup already marks >6h tasks failed, but
                # this process may keep running for days without restarting — a
                # phantom would otherwise block every future scheduled run).
                _running = _conn.execute(
                    "SELECT COUNT(*) FROM scraping_tasks "
                    "WHERE source='xunkexing' AND status='running' "
                    "AND started_at >= datetime('now', ?)",
                    (f"-{STALE_RUNNING_HOURS} hours",),
                ).fetchone()
                if _running and _running[0] > 0:
                    logger.info(
                        "Collection skipped: another collection already in progress "
                        f"({_running[0]} running task(s) within "
                        f"{STALE_RUNNING_HOURS}h) — scheduled run yielding to it"
                    )
                    return
            finally:
                _conn.close()
    except Exception:
        logger.exception("Concurrency guard failed — proceeding with collection")

    from collector.monitor import HealthMonitor
    from collector.segments import SegmentManager
    from collector.sources.xunkexing import XunKeXingCollector
    from collector.pipeline import ingest_leads
    from crm.db import get_connection
    from crm.manager import LeadManager

    monitor = HealthMonitor(source="xunkexing")
    task_id = _log_task_start("xunkexing")

    # ── Initialize ALL variables used in exception handlers BEFORE the try ──
    # This prevents NameError cascades: if setup fails, the handler itself
    # must not crash because today_countries/leads_found/collector are undefined.
    today_countries: list[str] = []
    collector = None
    leads_found = 0
    leads_new = 0

    try:
        db_path = config.DB_PATH
        if not db_path.exists():
            from crm.db import build_database
            build_database(db_path)

        conn = get_connection(db_path)

        try:
            lead_mgr = LeadManager(conn)
            segment_mgr = SegmentManager(conn)

            # Get the full country list from the collector's defaults
            from collector.sources.xunkexing import (
                DEFAULT_COUNTRY_KEYS, ALL_COUNTRY_KEYS,
            )

            # Determine country list based on config
            use_all = getattr(config, 'COLLECTION_USE_ALL_COUNTRIES', False)
            all_countries = ALL_COUNTRY_KEYS if use_all else DEFAULT_COUNTRY_KEYS

            # Select today's segment (or all if segment_size=0)
            segment_size = config.COLLECTION_SEGMENT_SIZE
            if segment_size <= 0 or segment_size >= len(all_countries):
                today_countries = list(all_countries)
                logger.info(
                    f"Segment disabled: collecting ALL {len(today_countries)} countries"
                )
            else:
                today_countries = segment_mgr.select_today_countries(
                    all_countries,
                    segment_size=segment_size,
                )
                logger.info(
                    f"Today's segment: {len(today_countries)}/{len(all_countries)} "
                    f"countries — {today_countries}"
                )

            monitor.start_run(countries_total=len(today_countries))

            # Determine search mode (keyword search is primary; industry filter is broken)
            use_keywords = getattr(config, 'COLLECTION_USE_KEYWORD_SEARCH', False)
            max_per = getattr(config, 'COLLECTION_MAX_PER_COUNTRY', 200)

            # ── Full-scan interval gating ─────────────────────────────────
            # Every N days: full country×keyword matrix (207×40 = 8280 queries).
            # On off-days: incremental mode — unfiltered + Tier4 long-tail
            # keywords, which catch new registrations without re-scanning the
            # saturated Tier1-3 keywords.
            # "Last full scan" is tracked via the search_mode column (not the
            # old leads_found >= 5000 heuristic — an incremental unfiltered run
            # also finds ~7.8k leads and was being mistaken for a full scan,
            # so the 40-keyword matrix never ran again after 2026-07-27).
            full_scan_interval = getattr(config, 'COLLECTION_FULL_SCAN_INTERVAL_DAYS', 1)
            if use_keywords and full_scan_interval > 1:
                # Check when the last full scan ran
                last_full = conn.execute(
                    """SELECT started_at FROM scraping_tasks
                       WHERE source='xunkexing' AND status IN ('completed', 'failed')
                       AND search_mode='full'
                       ORDER BY started_at DESC LIMIT 1"""
                ).fetchone()
                if last_full and last_full[0]:
                    try:
                        # started_at is stored in UTC (see _log_task_start) —
                        # compare against UTC now so the age isn't skewed by
                        # the local timezone offset.
                        last_dt = datetime.strptime(last_full[0], "%Y-%m-%d %H:%M:%S")
                        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
                        days_since = (utc_now - last_dt).days
                        if days_since < full_scan_interval:
                            logger.info(
                                f"Incremental mode: last full scan {days_since}d ago "
                                f"(interval={full_scan_interval}d) — "
                                f"running unfiltered+Tier4 scan to catch new registrations"
                            )
                            use_keywords = False
                        else:
                            logger.info(
                                f"Full scan mode: last full scan {days_since}d ago "
                                f"(interval={full_scan_interval}d)"
                            )
                    except (ValueError, TypeError):
                        logger.warning("Could not parse last full scan date — running full scan")

            # Record this run's scan mode so the gating above works on the
            # next run (an incremental run must NOT count as a full scan).
            search_mode = "full" if use_keywords else "incremental"
            try:
                conn.execute(
                    "UPDATE scraping_tasks SET search_mode=? WHERE id=?",
                    (search_mode, task_id),
                )
                conn.commit()
            except Exception:
                logger.exception("Failed to record search_mode for task %s", task_id)

            # Run collector with expanded settings.
            # Incremental days additionally scan the Tier4 long-tail keywords
            # (single source of truth: xunkexing.FASTENER_KEYWORDS_TIER4)
            # instead of the unfiltered-only scan that returns 0 new leads.
            collector_kwargs = dict(
                country_keys=today_countries,
                headless=True,
                use_all_countries=use_all,
                use_keyword_search=use_keywords,
                max_per_country=max_per,
            )
            if not use_keywords and getattr(
                config, 'COLLECTION_INCREMENTAL_KEYWORDS_ENABLED', True
            ):
                from collector.sources.xunkexing import FASTENER_KEYWORDS_TIER4
                collector_kwargs["keywords"] = FASTENER_KEYWORDS_TIER4
            collector = XunKeXingCollector(**collector_kwargs)

            try:
                # Single asyncio.run() for BOTH collection and cleanup.
                # On Windows ProactorEventLoop, calling asyncio.run() a
                # second time in a finally block creates a new loop that
                # crashes when pending transport-close callbacks fire
                # during teardown. Running both in one context ensures
                # cleanup happens while the original loop is still alive.
                raw_leads = asyncio.run(_collect_and_close(collector))
                leads_found = len(raw_leads)

                # Get per-country results from the collector
                country_results = getattr(collector, '_country_results', {})

                # Record each country's outcome
                for ck in today_countries:
                    cr = country_results.get(ck, {})
                    success = cr.get("success", True)
                    c_leads = cr.get("leads_found", 0)

                    monitor.record_country_result(ck, success=success, leads_found=c_leads)
                    segment_mgr.record_collection(
                        ck,
                        leads_found=c_leads,
                        had_error=not success,
                    )

                # Get rate limiter stats for monitoring
                rl_stats = collector._rate_limiter.get_stats()
                if rl_stats.get("circuit_open"):
                    monitor.record_circuit_breaker()

                # Ingest into CRM
                if raw_leads:
                    result = ingest_leads(raw_leads, "xunkexing", lead_mgr)
                    leads_new = result.get("created", 0)
                    logger.info(
                        f"Ingestion: {leads_found} raw → {leads_new} new, "
                        f"{result.get('duplicates_skipped', 0)} duplicates"
                    )

                    # ── Saturation warning ───────────────────────────────
                    # <1% of fetched leads are new → the platform pool is
                    # effectively swept. Surface it so the operator knows to
                    # switch/extend sources instead of seeing silent 0s.
                    if leads_found > 0 and leads_new / leads_found < 0.01:
                        logger.warning(
                            "寻客星源饱和告警: new/found=%.2f%% (%d/%d) — "
                            "平台新增不足, 建议切换/新增来源 "
                            "(诊断: python tools/audit.py)",
                            100.0 * leads_new / leads_found,
                            leads_new,
                            leads_found,
                        )

                monitor.end_run(leads_found=leads_found, leads_new=leads_new)

            except Exception as e:
                logger.exception("Collection failed mid-run")
                monitor.end_run(leads_found=leads_found, leads_new=leads_new, error=str(e))
                # Record remaining countries as failed
                for ck in today_countries:
                    if ck not in monitor._country_details:
                        monitor.record_country_result(ck, success=False, leads_found=0)
                        segment_mgr.record_collection(ck, leads_found=0, had_error=True)
                # Re-raise so the outer except block records the task as failed
                raise

            # Save health metrics
            monitor.save_to_db(conn, task_id=task_id)

            # Log task completion
            error_msg = None
            if monitor.error_count > 0:
                error_msg = (
                    f"Errors: {monitor.error_count}/{monitor.total_requests} requests. "
                    f"Health: {monitor.compute_health_score():.0f}/100"
                )

            _log_task_complete(
                task_id=task_id,
                leads_found=leads_found,
                leads_new=leads_new,
                error=error_msg,
                countries_collected=monitor._countries_collected,
                countries_failed=monitor._countries_failed,
            )

        finally:
            conn.close()

        # Summary
        health = monitor.compute_health_score()
        summary = monitor.get_summary()
        logger.info(
            f"SCHEDULED COLLECTION COMPLETE: "
            f"{summary['countries_collected']}/{summary['countries_total']} countries, "
            f"{leads_found} raw → {leads_new} new leads, "
            f"health={health:.0f}/100"
        )

        # Log token status for diagnostics (visible in logs without
        # needing to inspect the JSON file)
        token_info = getattr(collector, '_token_status', None)
        if token_info:
            logger.info(
                f"Token status: source={token_info.get('source')}, "
                f"valid={token_info.get('valid')}, "
                f"detail={token_info.get('detail')}"
            )
            if not token_info.get("valid", False):
                logger.warning(
                    "Token is invalid or expired — next run may require "
                    "manual re-login: python -m collector.setup xunkexing"
                )

    except Exception as e:
        logger.exception("SCHEDULED COLLECTION FAILED")
        # Include the actual exception type + message so we can diagnose from DB
        error_detail = f"Fatal scheduler error: {type(e).__name__}: {e}"[:500]
        _log_task_complete(
            task_id=task_id,
            leads_found=leads_found,
            leads_new=leads_new,
            error=error_detail,
        )

    finally:
        # ── Email batch: runs after every collection (pass or fail) ─────────
        # Must run in a background thread, NOT inline here: this function is
        # executing in the APScheduler worker thread right after
        # asyncio.run(_collect_and_close) returned, so the Windows
        # ProactorEventLoop may still be tearing down. Running
        # asyncio.run() again inline makes aiosmtplib's futures collide with
        # the closing loop → "Event loop shutting down", silently dropping
        # the whole batch (observed 2026-08-04 15:58: 3 sends, 0 completed).
        # A separate daemon thread gets a clean event loop, same as the
        # APScheduler email job and the catch-up path.
        #
        # 小时门禁（08-17 双批次修复）：只有当当前时间已过 EMAIL_SCHEDULE_HOUR
        # 才链式触发——采集在发信时间之前跑完时，cron job 会在正点自己触发，
        # 提前链式触发会让同一个批跑两次；采集跑晚了（发信时间已过）才由
        # 这里兜底补发。即便两个入口仍同时触发，batch_run_guards 的原子
        # claim 也会让第二个入口直接跳过。
        if (
            config.EMAIL_SCHEDULE_ENABLED
            and _is_weekday()
            and datetime.now().hour >= config.EMAIL_SCHEDULE_HOUR
        ):
            try:
                threading.Thread(
                    target=_run_scheduled_email_batch, daemon=True
                ).start()
            except Exception:
                logger.exception("Email batch failed (non-fatal — collection OK)")
        else:
            logger.info(
                "Weekend, email disabled, or before the email hour — collection "
                "finished without chained email batch"
            )

        # Reschedule for a random time tomorrow
        _reschedule_for_tomorrow()


def _reschedule_for_tomorrow():
    """Pick a random time for tomorrow's run and update the scheduler."""
    global _scheduler, _next_run_info

    if _scheduler is None:
        return

    hour, minute = _pick_random_time()
    _next_run_info = {
        "hour": hour,
        "minute": minute,
        "scheduled_at": _format_next_run(hour, minute),
    }

    try:
        _scheduler.reschedule_job(
            _current_job_id,
            trigger=CronTrigger(hour=hour, minute=minute),
        )
        logger.info(
            f"Rescheduled: next run at {hour:02d}:{minute:02d} "
            f"({_next_run_info['scheduled_at']})"
        )
    except Exception:
        logger.exception("Failed to reschedule job")


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled Facebook Collection
# ═══════════════════════════════════════════════════════════════════════════

_fb_job_id = "fb_collection"
_fb_next_run: dict = {"hour": None, "minute": None, "scheduled_at": None}


def _run_facebook_collection():
    """Run Facebook collection — lighter than xunkexing, no country segments.

    Facebook target: public posts/groups/pages with fastener-industry keywords.
    Session persistence: uses data/fb_profile/ (login once, session persists).

    Scheduled every N days (config.FB_COLLECTION_INTERVAL_DAYS) to avoid
    triggering Facebook's anti-bot detection with daily runs.
    """
    logger.info("=" * 60)
    logger.info("FACEBOOK COLLECTION STARTING")
    logger.info("=" * 60)

    from collector.sources.facebook import FacebookCollector
    from collector.pipeline import ingest_leads
    from crm.db import get_connection
    from crm.manager import LeadManager

    task_id = _log_task_start("facebook")
    leads_found = 0
    leads_new = 0
    collector = None

    try:
        # ── Interval guard: only run every N days ────────────────────────
        db_path = config.DB_PATH
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    """SELECT started_at FROM scraping_tasks
                       WHERE source='facebook' AND status='completed'
                       ORDER BY started_at DESC LIMIT 1"""
                ).fetchone()
                if row:
                    from datetime import timedelta
                    last_run = row[0]
                    if last_run:
                        last_dt = datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S")
                        days_since = (datetime.now() - last_dt).days
                        if days_since < config.FB_COLLECTION_INTERVAL_DAYS:
                            logger.info(
                                f"Facebook collection skipped: last run {days_since}d ago "
                                f"(interval={config.FB_COLLECTION_INTERVAL_DAYS}d)"
                            )
                            _log_task_complete(
                                task_id, leads_found=0, leads_new=0,
                                error=f"Skipped ({days_since}d since last run)",
                            )
                            return
            finally:
                conn.close()

        # Verify profile exists (login required for first run)
        from collector.sources.facebook import FB_PROFILE_DIR
        if not FB_PROFILE_DIR.exists():
            logger.warning(
                "Facebook profile not found at %s — skipping. "
                "Run setup: python -m collector.setup facebook",
                FB_PROFILE_DIR,
            )
            _log_task_complete(
                task_id, leads_found=0, leads_new=0,
                error="No FB profile (run: python -m collector.setup facebook)",
            )
            return

        db_path = config.DB_PATH
        if not db_path.exists():
            from crm.db import build_database
            build_database(db_path)

        collector = FacebookCollector(
            headless=False,  # FB blocks headless — must be visible
            max_posts=config.FB_COLLECTION_MAX_LEADS,
        )

        try:
            raw_leads = asyncio.run(collector.collect())
        finally:
            # Close Playwright browser (resource cleanup)
            try:
                asyncio.run(collector.close())
            except Exception:
                pass

        leads_found = len(raw_leads)
        if not raw_leads:
            logger.info("Facebook collection returned 0 leads — session may need refresh")
            _log_task_complete(
                task_id, leads_found=0, leads_new=0,
                error="0 leads (session may need refresh: python -m collector.setup facebook)",
            )
            return

        # Ingest into CRM
        conn = get_connection(db_path)
        try:
            lead_mgr = LeadManager(conn)
            result = ingest_leads(raw_leads, "facebook", lead_mgr)
            leads_new = result.get("created", 0)
            logger.info(
                f"Facebook ingestion: {leads_found} raw → {leads_new} new, "
                f"{result.get('duplicates_skipped', 0)} duplicates"
            )
        finally:
            conn.close()

        _log_task_complete(
            task_id=task_id,
            leads_found=leads_found,
            leads_new=leads_new,
        )

        logger.info(
            f"FACEBOOK COLLECTION COMPLETE: "
            f"{leads_found} raw → {leads_new} new leads"
        )

    except Exception as e:
        logger.exception("FACEBOOK COLLECTION FAILED")
        _log_task_complete(
            task_id=task_id,
            leads_found=leads_found,
            leads_new=leads_new,
            error=f"Fatal: {type(e).__name__}: {e}"[:500],
        )


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled Email Batch
# ═══════════════════════════════════════════════════════════════════════════

_email_job_id = "daily_email_batch"
_email_next_run: dict = {"hour": None, "minute": None, "scheduled_at": None}

_imap_job_id = "daily_inbox_check"
_imap_next_run: dict = {"hour": None, "minute": None, "scheduled_at": None}

_followup_job_id = "daily_followup_check"
_followup_next_run: dict = {"hour": None, "minute": None, "scheduled_at": None}

_zoho_sent_job_id = "zoho_sent_tracker"

_zoho_batch_job_id = "zoho_daily_batch"
_zoho_batch_next_run: dict = {"hour": None, "minute": None, "scheduled_at": None}


def _run_scheduled_email_batch():
    """每日自动邮件批次入口（带互斥守卫）。

    采集 finally 链、cron job、启动 catch-up 三个入口在同一天可能各自触发
    一次（08-17 曾双批次并发 → 重复发信）。batch_run_guards 原子 claim
    保证每天 kind='email' 至多一个批次真正运行，其余入口只记日志跳过。
    手动 UI/API/CLI 发送直接调 emailer.batch，不经过本函数，不受影响。
    """
    from datetime import date as _date

    if not _is_weekday():
        logger.info("Weekend — skipping automatic email batch (manual sends unaffected)")
        return

    today = _date.today().isoformat()
    with _EMAIL_BATCH_LOCK:
        if not _claim_batch_run("email", today):
            logger.info(
                f"Email batch for {today} already claimed (running or done) — "
                f"skipping duplicate trigger"
            )
            return
    try:
        _do_scheduled_email_batch()
    finally:
        _finish_batch_run("email", today)


def _do_scheduled_email_batch():
    """批次实际发信体——仅在互斥守卫内调用（星期守卫由调用方承担）。"""
    from datetime import date as _date

    # ── Guard: skip if already hit today's quota ─────────────────────────
    db_path = config.DB_PATH
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT email_count FROM sending_quotas WHERE date = ?",
                (_date.today().isoformat(),),
            ).fetchone()
            if row and row[0] >= config.DAILY_EMAIL_LIMIT:
                logger.info(
                    f"Email quota already reached today "
                    f"({row[0]}/{config.DAILY_EMAIL_LIMIT}) — skipping batch"
                )
                return
        finally:
            conn.close()

    logger.info("=" * 60)
    logger.info("SCHEDULED EMAIL BATCH STARTING")
    logger.info("=" * 60)

    try:
        from emailer.batch import send_batch, ZOHO_FUMENG_SOURCES

        # send_batch is fully synchronous (stdlib smtplib) — no asyncio.run.
        # The async wrapper repeatedly hit "cannot schedule new futures after
        # shutdown" on Windows when the batch loop ran concurrently with the
        # asyncio crawls in the same process (observed 2026-08-05: died at
        # email #29, ~55 of ~83 failed). Sync SMTP removes the loop entirely.
        #
        # exclude_sources: the 孚盟 customs-data pools belong to the Zoho
        # mailbox (see _run_scheduled_zoho_batch). Without this, the NetEase
        # batch would consume them by priority + discovered_at DESC.
        results = send_batch(
            limit=config.EMAIL_BATCH_SIZE,
            dry_run=False,
            min_email_quality=True,
            exclude_sources=list(ZOHO_FUMENG_SOURCES),
        )

        if results.get("fatal"):
            logger.error(f"Email batch fatal error: {results['fatal']}")
            return

        sent = results.get("sent", 0)
        skipped = results.get("skipped", 0)
        errors = results.get("errors", 0)

        logger.info(
            f"SCHEDULED EMAIL BATCH COMPLETE: "
            f"{sent} sent, {skipped} skipped, {errors} errors"
        )

        # Log per-error details
        for d in results.get("details", []):
            if d["status"] == "error":
                logger.warning(
                    f"  EMAIL FAILED: {d['company']} ({d['email']}) — "
                    f"{d.get('reason', 'unknown')}"
                )
            elif d["status"] == "skipped" and "Daily limit" in d.get("reason", ""):
                logger.info(f"  Daily cap reached — remaining skipped")

    except Exception:
        logger.exception("SCHEDULED EMAIL BATCH FAILED")


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled Zoho 孚盟 Batch
# ═══════════════════════════════════════════════════════════════════════════


def _run_scheduled_zoho_batch():
    """Zoho 孚盟批次入口（带互斥守卫，与 NetEase 批次同款）。

    cron job 与启动 catch-up 两个入口同一天可能各自触发一次；batch_run_guards
    保证每天 kind='zoho' 至多一个批次真正运行。配额独立于 NetEase 批次
    （mailbox_quotas vs sending_quotas），两通道按设计可并行。
    """
    from datetime import date as _date

    if not config.ZOHO_SMTP_USERNAME or not config.ZOHO_SMTP_PASSWORD:
        logger.warning(
            "Zoho SMTP credentials not configured (FTA_ZOHO_SMTP_USERNAME/PASSWORD) "
            "— skipping 孚盟 batch"
        )
        return

    if not _is_weekday():
        logger.info("Weekend — skipping automatic Zoho 孚盟 batch")
        return

    today = _date.today().isoformat()
    with _ZOHO_BATCH_LOCK:
        if not _claim_batch_run("zoho", today):
            logger.info(
                f"Zoho 孚盟 batch for {today} already claimed (running or done) — "
                f"skipping duplicate trigger"
            )
            return
    try:
        _do_scheduled_zoho_batch()
    finally:
        _finish_batch_run("zoho", today)


def _do_scheduled_zoho_batch():
    """批次实际发信体——仅在互斥守卫内调用。

    Drains 孚盟数据 (high quality) first, then 孚盟数据普通, capped by the
    Zoho 20/day quota (mailbox_quotas) — fully independent of the NetEase
    200/day batch (sending_quotas), so both channels can run in parallel on
    their own scheduler threads without blocking each other. Idempotent:
    skips if today's Zoho quota is already exhausted (manual CLI sends count
    against the same mailbox_quotas row).
    """
    from datetime import date as _date

    # ── Guard: skip if today's Zoho quota is already exhausted ──────────
    db_path = config.DB_PATH
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT email_count FROM mailbox_quotas "
                "WHERE date = ? AND mailbox = 'zoho'",
                (_date.today().isoformat(),),
            ).fetchone()
            if row and row[0] >= config.ZOHO_DAILY_LIMIT:
                logger.info(
                    f"Zoho quota already reached today "
                    f"({row[0]}/{config.ZOHO_DAILY_LIMIT}) — skipping 孚盟 batch"
                )
                return
        finally:
            conn.close()

    logger.info("=" * 60)
    logger.info("SCHEDULED ZOHO 孚盟 BATCH STARTING")
    logger.info("=" * 60)

    try:
        from emailer.batch import run_zoho_daily_batch

        res = run_zoho_daily_batch()
        if res.get("fatal"):
            logger.error(f"Zoho 孚盟 batch fatal error: {res['fatal']}")
            return

        logger.info(
            "SCHEDULED ZOHO 孚盟 BATCH COMPLETE: "
            f"sent={res['sent']}, skipped={res['skipped']}, errors={res['errors']}"
        )
        for p in res.get("pools", []):
            logger.info(
                f"  pool {p['source']}: {p['sent']} sent, "
                f"{p['skipped']} skipped, {p['errors']} errors"
            )
    except Exception:
        logger.exception("SCHEDULED ZOHO 孚盟 BATCH FAILED")


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled IMAP Inbox Check
# ═══════════════════════════════════════════════════════════════════════════


def _run_scheduled_inbox_check():
    """Check IMAP inbox for customer replies and bounces.

    Connects via IMAP, classifies incoming messages, matches replies to sent
    emails via Message-ID, and updates lead/customer statuses.

    Idempotent: the 7-layer filter prevents duplicate processing of already-seen
    messages (checked via imap_uid in communications table).
    """
    logger.info("=" * 60)
    logger.info("SCHEDULED INBOX CHECK STARTING")
    logger.info("=" * 60)

    db_path = config.DB_PATH
    if not db_path.exists():
        logger.warning("DB not found — skipping inbox check")
        return

    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        from emailer.inbox import InboxMonitor

        monitor = InboxMonitor(conn)
        result = monitor.check_inbox(hours_back=config.IMAP_CHECK_INTERVAL_HOURS)

        if result.errors:
            for err in result.errors:
                logger.error(f"  INBOX ERROR: {err}")

        reply_count = len(result.replies)
        logger.info(
            f"SCHEDULED INBOX CHECK COMPLETE: "
            f"{result.checked} scanned, {reply_count} replies, "
            f"{result.bounces} bounces, {result.auto_replies} auto-replies, "
            f"{result.unknown} unknown"
        )

        for r in result.replies:
            logger.info(
                f"  REPLY: {r.from_email} — lead={r.lead_id} "
                f"method={r.match_method} subject={r.subject[:60] if r.subject else 'N/A'}"
            )

    except Exception:
        logger.exception("SCHEDULED INBOX CHECK FAILED")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled Follow-Up Automation
# ═══════════════════════════════════════════════════════════════════════════


def _run_scheduled_followups():
    """Run multi-stage follow-up automation.

    Queries leads whose icebreaker was sent 3-35 days ago with no reply,
    sends the appropriate follow-up template for each stage, and marks
    leads as dead after all stages are exhausted.
    """
    # ── Weekday guard: follow-up check Sends emails → 工作日仅 ────────────
    if not _is_weekday():
        logger.info("Weekend — skipping automatic follow-up check (sends follow-up emails)")
        return

    logger.info("=" * 60)
    logger.info("SCHEDULED FOLLOW-UP CHECK STARTING")
    logger.info("=" * 60)

    db_path = config.DB_PATH
    if not db_path.exists():
        logger.warning("DB not found — skipping follow-up check")
        return

    try:
        from engine.followup import FollowUpEngine

        engine = FollowUpEngine(db_path, dry_run=False)
        total = engine.run()

        logger.info(
            "SCHEDULED FOLLOW-UP COMPLETE: "
            f"stages={len(total['stages'])}, "
            f"{total['total_candidates']} candidates, "
            f"{total['total_sent']} sent, "
            f"{total['total_skipped']} skipped, "
            f"{total['total_errors']} errors, "
            f"{total['total_dead']} dead"
        )

        for s in total["stages"]:
            if s["candidates"] > 0:
                logger.info(
                    f"  Stage {s['stage']} ({s['description']}): "
                    f"{s['candidates']} candidates → "
                    f"{s['sent']} sent, {s['skipped']} skipped, {s['errors']} errors"
                )

        if total["total_dead"] > 0:
            logger.info(f"  Marked {total['total_dead']} leads as dead")

    except Exception:
        logger.exception("SCHEDULED FOLLOW-UP CHECK FAILED")


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled Zoho Sent-Follow-Up Tracker
# ═══════════════════════════════════════════════════════════════════════════


def _run_scheduled_zoho_sent():
    """Detect the user's manual Zoho follow-ups and record them into the CRM.

    Scans today's Zoho Sent folder, classifies recipients (老客户/询过价 vs
    新联系 via Zoho Inbox history), records outbound communications + lead
    updates, and writes a Markdown report.

    Fail-soft: never raises into the scheduler thread. Skips entirely if
    Zoho credentials are not configured.
    """
    if not config.ZOHO_IMAP_USERNAME or not config.ZOHO_IMAP_PASSWORD:
        logger.warning(
            "Zoho credentials not configured (FTA_ZOHO_IMAP_USERNAME/PASSWORD) "
            "— skipping sent tracker"
        )
        return

    logger.info("=" * 60)
    logger.info("ZOHO SENT-FOLLOW-UP TRACKER STARTING")
    logger.info("=" * 60)

    db_path = config.DB_PATH
    if not db_path.exists():
        logger.warning("DB not found — skipping Zoho sent tracker")
        return

    conn = None
    try:
        from engine.sent_tracker import ZohoSentTracker

        conn = sqlite3.connect(str(db_path))
        tracker = ZohoSentTracker(conn, log_dir=config.LOG_DIR)
        result = tracker.run()

        for err in result.errors:
            logger.error(f"  ZOHO SENT ERROR: {err}")
        logger.info(
            "ZOHO SENT-FOLLOW-UP COMPLETE: "
            f"{result.scanned} scanned, {result.recipients_total} recipients, "
            f"{len(result.veterans)} 老客户, {len(result.new_contacts)} 新联系, "
            f"{result.leads_created} leads created, {result.leads_updated} updated, "
            f"{result.comms_recorded} comms, {result.deduped} deduped"
        )
        if result.report_path:
            logger.info("Zoho sent report: %s", result.report_path)
    except Exception:
        logger.exception("ZOHO SENT-FOLLOW-UP TRACKER FAILED")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# Saturday Self-Check Job
# ═══════════════════════════════════════════════════════════════════════════


def _weekly_summary_md() -> str:
    """本周(近7天)数据汇总 — 只读 SQL, 输出 markdown 行."""
    try:
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    except Exception:
        return "(DB 不可读, 跳过周报)"
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()

        def _one(sql: str, params=()) -> int:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else 0

        _7d = "datetime('now','-7 days')"
        q_sent = ("SELECT COUNT(*) FROM communications WHERE direction='outbound' "
                  "AND channel='email' AND status='sent' AND sent_at >= " + _7d)
        q_quota = "SELECT COALESCE(SUM(email_count),0) FROM sending_quotas " \
                  "WHERE date >= date('now','-7 days')"
        q_followup = ("SELECT COUNT(*) FROM communications WHERE direction='outbound' "
                      "AND template_id LIKE 'followup_%' AND sent_at >= " + _7d)
        q_reply = ("SELECT COUNT(*) FROM communications WHERE direction='inbound' "
                   "AND sent_at >= " + _7d)
        q_new = "SELECT COUNT(*) FROM leads WHERE discovered_at >= " + _7d
        q_dup = "SELECT COUNT(*) FROM leads WHERE duplicate_of IS NOT NULL"

        out = []
        out.append(f"- 本周已发邮件(comm status=sent): {_one(q_sent)}")
        out.append(f"- 本周发送配额计数: {_one(q_quota)}")
        for row in cur.execute(
            "SELECT source, COUNT(*) runs, SUM(leads_found) found, SUM(leads_new) new "
            "FROM scraping_tasks WHERE started_at >= " + _7d + " GROUP BY source"
        ):
            out.append(
                f"- 采集 {row['source']}: {row['runs']}次, "
                f"found={row['found'] or 0}, new={row['new'] or 0}"
            )
        out.append(f"- 本周跟进邮件: {_one(q_followup)}")
        out.append(f"- 本周回复/收件: {_one(q_reply)}")
        out.append(f"- 本周新增线索: {_one(q_new)}")
        out.append(f"- duplicate_of 标记线索: {_one(q_dup)}")
        return "\n".join(out)
    except Exception as e:
        return f"(周报查询失败: {type(e).__name__}: {e})"
    finally:
        conn.close()


def _run_saturday_self_check():
    """周六自查: 数据源审计 + IMAP 诊断 + 回归测试 + 日志/DB 健康与清理 +
    残留进程扫描 + 本周数据汇总 + git 状态。输出 logs/saturday_report_YYYY-MM-DD.md。

    每节独立 try/except — 单节失败不阻断报告生成。
    """
    import json
    import subprocess
    import sys
    from datetime import date as _date

    report_path = config.LOG_DIR / f"saturday_report_{_date.today():%Y-%m-%d}.md"
    lines: list[str] = [f"# FTA 周六自查报告 — {_date.today():%Y-%m-%d}"]

    def emit(s: str = "") -> None:
        lines.append(s)

    logger.info("SATURDAY SELF-CHECK STARTING")

    # 1) 数据源健康(复用 tools.audit, 只读)
    try:
        from tools.audit import audit, _md
        emit("\n## 1. 数据源健康\n" + _md(audit()))
    except Exception as e:
        emit(f"\n## 1. 数据源健康\n[ERROR] {type(e).__name__}: {e}")

    # 2) IMAP 诊断(复用 engine.diagnostics, 只读)
    try:
        from engine.diagnostics import run_diagnostics
        d = run_diagnostics(check_inbox=True, check_sent=True)
        emit(f"\n## 2. IMAP 诊断\n{json.dumps(d, ensure_ascii=False, indent=2, default=str)}")
    except Exception as e:
        emit(f"\n## 2. IMAP 诊断\n[ERROR] {type(e).__name__}: {e}")

    # 3) 回归测试(subprocess pytest, conftest 已隔离生产 DB)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short"],
            cwd=config.BASE_DIR,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        tail = "\n".join((r.stdout or "").splitlines()[-25:])
        emit(f"\n## 3. 回归测试 (exit={r.returncode})\n```\n{tail}\n```")
    except subprocess.TimeoutExpired:
        emit("\n## 3. 回归测试\n[TIMEOUT] pytest >30min, 请手动运行确认")
    except Exception as e:
        emit(f"\n## 3. 回归测试\n[ERROR] {type(e).__name__}: {e}")

    # 4) 日志/DB 健康与清理(tools/cleanup)
    try:
        from tools.cleanup import clean_logs, cleanup_db_retention, db_size_report
        emit("\n## 4. 日志/DB 健康\n" + db_size_report())
        n = clean_logs(retention_days=config.LOG_RETENTION_DAYS, dry_run=False)
        m = cleanup_db_retention(
            retention_days=config.MONITOR_RETENTION_DAYS, dry_run=False
        )
        emit(
            f"\n已清理旧日志 {n} 个, collection_health 旧记录 {m} 行 "
            f"(LOG_RETENTION={config.LOG_RETENTION_DAYS}d, "
            f"MONITOR_RETENTION={config.MONITOR_RETENTION_DAYS}d)"
        )
    except Exception as e:
        emit(f"\n## 4. 日志/DB 健康\n[ERROR] {type(e).__name__}: {e}")

    # 5) 残留进程扫描
    try:
        from tools.cleanup import scan_zombie_processes
        z = scan_zombie_processes()
        emit(f"\n## 5. 残留进程\n" + (z or "(无)"))
    except Exception as e:
        emit(f"\n## 5. 残留进程\n[ERROR] {type(e).__name__}: {e}")

    # 6) 本周数据汇总
    emit("\n## 6. 本周数据汇总\n" + _weekly_summary_md())

    # 7) git 未提交改动提醒
    try:
        g = subprocess.run(
            ["git", "status", "--short"],
            cwd=config.BASE_DIR,
            capture_output=True,
            text=True,
        ).stdout.strip()
        emit("\n## 7. git 状态\n" + (g if g else "(工作区干净)"))
        if "librarian.py" in g:
            emit("\n> 注意: moma-lib/librarian.py 有未提交改动(不在本次范围, 待处理)")
    except Exception as e:
        emit(f"\n## 7. git 状态\n[ERROR] {type(e).__name__}: {e}")

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        logger.exception("Failed to write Saturday self-check report")

    logger.info("SATURDAY SELF-CHECK COMPLETE → %s", report_path)


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


def _mark_stale_running_tasks() -> int:
    """Mark scraping_tasks left in 'running' state by dead processes as failed.

    At startup every 'running' row older than STALE_RUNNING_HOURS is by
    definition stale — the process that started it is no longer alive (the
    current process just started and has no running tasks of its own). Without
    this cleanup the catch-up guard sees a phantom "collection already in
    progress" and skips the day's run forever.

    Returns the number of tasks marked stale.
    """
    db_path = config.DB_PATH
    if not db_path.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                """UPDATE scraping_tasks
                   SET status='failed', completed_at=datetime('now'),
                       error_message=COALESCE(error_message, '') || ' [stale: process restarted]'
                   WHERE status='running'
                     AND started_at < datetime('now', ?)""",
                (f"-{STALE_RUNNING_HOURS} hours",),
            )
            n = cur.rowcount
            conn.commit()
            if n:
                logger.warning(
                    f"Marked {n} stale running task(s) as failed "
                    f"(> {STALE_RUNNING_HOURS}h old)"
                )
            return n
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to clean stale running tasks")
        return 0


def start_scheduler(
    run_immediately: bool = False,
) -> BackgroundScheduler:
    """Start the background scheduler with randomized daily collection.

    The first run is scheduled at a random time within the configured window.
    After each run completes, the next day's time is re-randomized.

    Args:
        run_immediately: If True, run a collection cycle immediately on startup.

    Returns:
        The BackgroundScheduler instance.
    """
    global _scheduler, _next_run_info

    if _scheduler is not None:
        logger.warning("Scheduler is already running")
        return _scheduler

    # Clean up tasks left 'running' by a previously-killed process before the
    # catch-up guard decides whether another collection is in progress.
    _mark_stale_running_tasks()

    # Retention cleanup: purge collection_health rows older than
    # MONITOR_RETENTION_DAYS (dead config until now — see config.py).
    try:
        from tools.cleanup import cleanup_db_retention
        cleanup_db_retention(retention_days=config.MONITOR_RETENTION_DAYS, dry_run=False)
    except Exception:
        logger.exception("Startup DB retention cleanup failed")

    # Check if sources are available
    from collector.cli import SOURCE_REGISTRY
    if not SOURCE_REGISTRY:
        logger.warning("No sources registered — scheduler will be idle")
    else:
        logger.info(f"Sources registered: {list(SOURCE_REGISTRY.keys())}")

    # Pick a time only when the XunKeXing job is enabled.  Otherwise the
    # dashboard must not advertise a collection that cannot run.
    hour = minute = None
    if config.COLLECTION_XUNKEXING_ENABLED:
        hour, minute = _pick_random_time()
        formatted = _format_next_run(hour, minute)
        _next_run_info = {"hour": hour, "minute": minute, "scheduled_at": formatted}

        window = f"{config.SCHEDULE_TIME_WINDOW_START:02d}:00–{config.SCHEDULE_TIME_WINDOW_END:02d}:00"
        logger.info(
            f"Starting collection scheduler: "
            f"daily random in window [{window}], "
            f"first run at {hour:02d}:{minute:02d} ({formatted}), "
            f"segment size={config.COLLECTION_SEGMENT_SIZE}"
        )
    else:
        _next_run_info = {"hour": None, "minute": None, "scheduled_at": None, "paused": True}
        logger.info("Scheduled XunKeXing collection is paused")

    _scheduler = BackgroundScheduler(
        daemon=True,
        job_defaults={
            "misfire_grace_time": 7200,  # 2 hour grace period for time window
            "coalesce": True,
        },
    )

    if config.COLLECTION_XUNKEXING_ENABLED:
        _scheduler.add_job(
            _run_scheduled_collection,
            trigger=CronTrigger(hour=hour, minute=minute),
            id=_current_job_id,
            name="Daily XunKeXing Collection (Segmented + Randomized)",
            replace_existing=True,
        )
    else:
        logger.info("XunKeXing collection scheduler paused (FTA_XUNKEXING_COLLECTION_ENABLED=false)")

    # ── Email batch job ──────────────────────────────────────────────────
    email_start_date = _email_schedule_start_date()
    email_schedule_config_valid = (
        not config.EMAIL_SCHEDULE_START_DATE or email_start_date is not None
    )
    if config.EMAIL_SCHEDULE_ENABLED and email_schedule_config_valid:
        email_hour, email_minute = _pick_time_for_hour(config.EMAIL_SCHEDULE_HOUR)
        email_formatted = _format_next_run(email_hour, email_minute)
        _email_next_run = {
            "hour": email_hour,
            "minute": email_minute,
            "scheduled_at": email_formatted,
        }

        _scheduler.add_job(
            _run_scheduled_email_batch,
            trigger=CronTrigger(
                hour=email_hour,
                minute=email_minute,
                day_of_week="mon-fri",
                start_date=email_start_date,
            ),
            id=_email_job_id,
            name=f"Weekday Email Batch ({config.EMAIL_BATCH_SIZE}/day)",
            replace_existing=True,
        )
        logger.info(
            f"Email batch scheduler enabled: "
            f"weekday at {email_hour:02d}:{email_minute:02d}, "
            f"batch size={config.EMAIL_BATCH_SIZE}"
            + (f", start date={email_start_date.isoformat()}" if email_start_date else "")
        )
    elif config.EMAIL_SCHEDULE_ENABLED:
        logger.error("Email batch scheduler disabled: invalid start-date configuration")
    else:
        logger.info("Email batch scheduler disabled (FTA_EMAIL_SCHEDULE_ENABLED=false)")

    # ── Zoho 孚盟 batch job (weekdays, independent quota) ──────────────────
    # Runs on its own APScheduler worker thread → concurrent with the NetEase
    # batch above (separate mailbox, separate quota row, no shared budget).
    if config.ZOHO_BATCH_ENABLED:
        zb_hour, zb_minute = _pick_time_for_hour(config.ZOHO_BATCH_HOUR)
        zb_formatted = _format_next_run(zb_hour, zb_minute)
        _zoho_batch_next_run = {
            "hour": zb_hour,
            "minute": zb_minute,
            "scheduled_at": zb_formatted,
        }

        _scheduler.add_job(
            _run_scheduled_zoho_batch,
            trigger=CronTrigger(hour=zb_hour, minute=zb_minute, day_of_week="mon-fri"),
            id=_zoho_batch_job_id,
            name="Weekday Zoho 孚盟 Batch (孚盟数据 → 孚盟数据普通)",
            replace_existing=True,
        )
        logger.info(
            f"Zoho 孚盟 batch enabled: weekday at {zb_hour:02d}:{zb_minute:02d} "
            f"(quota={config.ZOHO_DAILY_LIMIT}/day, independent of NetEase)"
        )
    else:
        logger.info("Zoho 孚盟 batch disabled (FTA_ZOHO_BATCH_ENABLED=false)")

    # ── IMAP inbox check job ─────────────────────────────────────────────
    if config.IMAP_CHECK_ENABLED:
        # 业务时段(8–18 点)每 IMAP_CHECK_INTERVAL_HOURS 小时扫一次收件箱，
        # 而不是每天一次——08-20 实测下午到达的退信/回复只有等次日或重启
        # 才能被扫到（11:42 的每日检查之后到达的邮件当天不可见）。
        imap_step = max(1, int(config.IMAP_CHECK_INTERVAL_HOURS))
        imap_hours = list(range(8, 19, imap_step))
        _, imap_minute = _pick_time_for_hour(config.IMAP_CHECK_HOUR)
        # 精确展示"下一次"：取今天尚未到点的第一个小时，否则回落到明天首班。
        now_h, now_m = datetime.now().hour, datetime.now().minute
        next_h = next(
            (h for h in imap_hours if h > now_h or (h == now_h and imap_minute > now_m)),
            imap_hours[0],
        )
        imap_formatted = _format_next_run(next_h, imap_minute)
        _imap_next_run = {
            "hour": next_h,
            "minute": imap_minute,
            "scheduled_at": imap_formatted,
        }

        _scheduler.add_job(
            _run_scheduled_inbox_check,
            # APScheduler hour field takes a comma-separated string, not a list.
            trigger=CronTrigger(
                hour=",".join(str(h) for h in imap_hours),
                minute=imap_minute,
            ),
            id=_imap_job_id,
            name=(
                f"IMAP Inbox Check (every {imap_step}h "
                f"{imap_hours[0]}:xx–{imap_hours[-1]}:xx)"
            ),
            replace_existing=True,
        )
        logger.info(
            f"IMAP inbox check enabled: every {imap_step}h at "
            f"hours={imap_hours} (e.g. {imap_hours[0]}:{imap_minute:02d}), "
            f"lookback={config.IMAP_CHECK_INTERVAL_HOURS}h"
        )
    else:
        logger.info("IMAP inbox check disabled (FTA_IMAP_CHECK_ENABLED=false)")

    # ── Follow-up automation job ─────────────────────────────────────────
    if config.FOLLOWUP_SCHEDULE_ENABLED:
        fup_hour, fup_minute = _pick_time_for_hour(config.FOLLOWUP_CHECK_HOUR)
        fup_formatted = _format_next_run(fup_hour, fup_minute)
        _followup_next_run = {
            "hour": fup_hour,
            "minute": fup_minute,
            "scheduled_at": fup_formatted,
        }

        _scheduler.add_job(
            _run_scheduled_followups,
            trigger=CronTrigger(hour=fup_hour, minute=fup_minute, day_of_week="mon-fri"),
            id=_followup_job_id,
            name="Weekday Follow-Up Automation (stages 1-3 + dead marking)",
            replace_existing=True,
        )
        logger.info(
            f"Follow-up automation enabled: "
            f"weekday at {fup_hour:02d}:{fup_minute:02d} "
            f"(stages: 3-7d / 10-16d / 25-32d, dead >{35}d)"
        )
    else:
        _followup_next_run = {"hour": None, "minute": None, "scheduled_at": None}
        logger.info("Follow-up automation disabled (FTA_FOLLOWUP_SCHEDULE_ENABLED=false)")

    # ── Zoho sent-follow-up tracker job ───────────────────────────────────
    if config.ZOHO_SENT_ENABLED:
        zs_hour, zs_minute = _pick_time_for_hour(config.ZOHO_SENT_HOUR)
        zs_formatted = _format_next_run(zs_hour, zs_minute)
        _scheduler.add_job(
            _run_scheduled_zoho_sent,
            trigger=CronTrigger(hour=zs_hour, minute=zs_minute),
            id=_zoho_sent_job_id,
            name="Zoho Sent-Follow-Up Tracker (manual sends)",
            replace_existing=True,
        )
        logger.info(
            f"Zoho sent tracker enabled: "
            f"daily at {zs_hour:02d}:{zs_minute:02d} "
            f"(window={config.ZOHO_SENT_WINDOW_DAYS}d)"
        )
    else:
        logger.info("Zoho sent tracker disabled (FTA_ZOHO_SENT_ENABLED=false)")

    # ── Facebook collection job ────────────────────────────────────────────
    if config.FB_COLLECTION_ENABLED:
        fb_hour, fb_minute = _pick_time_for_hour(config.FB_COLLECTION_HOUR)
        fb_formatted = _format_next_run(fb_hour, fb_minute)
        _fb_next_run = {
            "hour": fb_hour,
            "minute": fb_minute,
            "scheduled_at": fb_formatted,
        }

        _scheduler.add_job(
            _run_facebook_collection,
            trigger=CronTrigger(hour=fb_hour, minute=fb_minute),
            id=_fb_job_id,
            name=f"Facebook Collection (every {config.FB_COLLECTION_INTERVAL_DAYS}d)",
            replace_existing=True,
        )
        logger.info(
            f"Facebook collection enabled: "
            f"daily check at {fb_hour:02d}:{fb_minute:02d}, "
            f"runs every {config.FB_COLLECTION_INTERVAL_DAYS} days, "
            f"max {config.FB_COLLECTION_MAX_LEADS} leads/run"
        )
    else:
        logger.info("Facebook collection disabled (FTA_FB_COLLECTION_ENABLED=false)")

    # ── Saturday self-check job ────────────────────────────────────────────
    if config.SATURDAY_CHECK_ENABLED:
        sc_hour, sc_minute = _pick_time_for_hour(config.SATURDAY_CHECK_HOUR)
        _scheduler.add_job(
            _run_saturday_self_check,
            trigger=CronTrigger(day_of_week="sat", hour=sc_hour, minute=sc_minute),
            id="saturday_self_check",
            name="Saturday Self-Check (audit + tests + cleanup + weekly summary)",
            replace_existing=True,
        )
        logger.info(
            f"Saturday self-check enabled: every Sat at {sc_hour:02d}:{sc_minute:02d} "
            f"(audit + tests + cleanup + weekly summary)"
        )
    else:
        logger.info("Saturday self-check disabled (FTA_SATURDAY_CHECK_ENABLED=false)")

    # ── Periodic stale-task cleanup ─────────────────────────────────────────
    # The startup stale-cleanup (_mark_stale_running_tasks) only runs once at
    # boot. If a collection is hard-killed (no graceful exit → atexit hook
    # skipped), its phantom 'running' row would otherwise block the concurrency
    # guard until the next restart. Re-run the age-based cleanup every 6h so a
    # phantom is cleared within one interval even if the app runs for weeks.
    _scheduler.add_job(
        _mark_stale_running_tasks,
        trigger=CronTrigger(hour="*/6", minute=13),
        id="periodic_stale_cleanup",
        name="Periodic stale-task cleanup (every 6h)",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Scheduler started")

    # ── Email catch-up: if past the email hour and quota not exhausted, run now ──
    # Weekday-only: 周六/周日重启应用不补发 (发送本身另有 _run_scheduled_email_batch
    # 顶部的 weekday 守卫兜底).
    if (
        config.EMAIL_SCHEDULE_ENABLED
        and _is_weekday()
        and _email_schedule_is_active()
    ):
        now = datetime.now()
        if now.hour >= config.EMAIL_SCHEDULE_HOUR:
            db_path = config.DB_PATH
            if db_path.exists():
                try:
                    conn = sqlite3.connect(str(db_path))
                    try:
                        row = conn.execute(
                            "SELECT email_count FROM sending_quotas WHERE date = ?",
                            (now.strftime("%Y-%m-%d"),),
                        ).fetchone()
                        sent_today = row[0] if row else 0
                    finally:
                        conn.close()

                    if sent_today < config.DAILY_EMAIL_LIMIT:
                        logger.info(
                            f"Catch-up: email time ({config.EMAIL_SCHEDULE_HOUR}:xx) "
                            f"passed, {sent_today}/{config.DAILY_EMAIL_LIMIT} sent today, "
                            f"starting batch in background..."
                        )
                        import threading
                        threading.Thread(
                            target=_run_scheduled_email_batch, daemon=True
                        ).start()
                    else:
                        logger.info(
                            f"Catch-up: daily quota already reached "
                            f"({sent_today}/{config.DAILY_EMAIL_LIMIT}) — skipping"
                        )
                except Exception:
                    logger.exception("Catch-up check failed — skipping email batch")

    # ── Zoho 孚盟 batch catch-up: if past its hour and Zoho quota not exhausted ──
    # Weekday-only, same convention as the NetEase batch catch-up.
    if config.ZOHO_BATCH_ENABLED and _is_weekday():
        now = datetime.now()
        if now.hour >= config.ZOHO_BATCH_HOUR:
            db_path = config.DB_PATH
            if db_path.exists():
                try:
                    conn = sqlite3.connect(str(db_path))
                    try:
                        row = conn.execute(
                            "SELECT email_count FROM mailbox_quotas "
                            "WHERE date = ? AND mailbox = 'zoho'",
                            (now.strftime("%Y-%m-%d"),),
                        ).fetchone()
                        zoho_sent = row[0] if row else 0
                    finally:
                        conn.close()

                    if zoho_sent < config.ZOHO_DAILY_LIMIT:
                        logger.info(
                            f"Catch-up: Zoho 孚盟 time ({config.ZOHO_BATCH_HOUR}:xx) "
                            f"passed, {zoho_sent}/{config.ZOHO_DAILY_LIMIT} sent today, "
                            f"starting batch in background..."
                        )
                        import threading
                        threading.Thread(
                            target=_run_scheduled_zoho_batch, daemon=True
                        ).start()
                    else:
                        logger.info(
                            f"Catch-up: Zoho 孚盟 quota already reached today "
                            f"({zoho_sent}/{config.ZOHO_DAILY_LIMIT}) — skipping"
                        )
                except Exception:
                    logger.exception("Catch-up check failed — skipping Zoho 孚盟 batch")

    # ── Collection catch-up: if past the collection window and no run today, start now ──
    now = datetime.now()
    if now.hour >= config.SCHEDULE_TIME_WINDOW_START:
        db_path = config.DB_PATH
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM scraping_tasks "
                        "WHERE source='xunkexing' AND status='completed' "
                        "AND started_at >= date('now')"
                    ).fetchone()
                    ran_today = row[0] > 0 if row else False

                    # Guard: don't start if another collection is already running
                    running = conn.execute(
                        "SELECT COUNT(*) FROM scraping_tasks "
                        "WHERE source='xunkexing' AND status='running'"
                    ).fetchone()
                    has_running = running[0] > 0 if running else False
                finally:
                    conn.close()

                if has_running:
                    logger.info(
                        "Catch-up: collection already in progress "
                        f"({running[0]} running task(s)) — skipping"
                    )
                elif not ran_today:
                    logger.info(
                        "Catch-up: collection window passed, "
                        "no successful run today — starting collection now"
                    )
                    import threading
                    threading.Thread(
                        target=_run_scheduled_collection, daemon=True
                    ).start()
                else:
                    logger.info(
                        f"Catch-up: collection already completed today "
                        f"({row[0]} run(s)) — waiting for tomorrow's window"
                    )
            except Exception:
                logger.exception("Catch-up check failed — skipping collection catch-up")

    # ── IMAP catch-up: if past the IMAP check hour, run inbox check now ──
    if config.IMAP_CHECK_ENABLED:
        now = datetime.now()
        if now.hour >= config.IMAP_CHECK_HOUR:
            db_path = config.DB_PATH
            if db_path.exists():
                try:
                    conn = sqlite3.connect(str(db_path))
                    try:
                        # Check if we already scanned inbox today (any inbound comm today)
                        row = conn.execute(
                            "SELECT COUNT(*) FROM communications "
                            "WHERE direction='inbound' AND sent_at >= date('now')"
                        ).fetchone()
                        has_inbound_today = row[0] > 0 if row else False
                    finally:
                        conn.close()

                    if not has_inbound_today:
                        logger.info(
                            f"Catch-up: IMAP check time ({config.IMAP_CHECK_HOUR}:xx) "
                            f"passed, no inbound mail today — checking inbox now"
                        )
                        import threading
                        threading.Thread(
                            target=_run_scheduled_inbox_check, daemon=True
                        ).start()
                    else:
                        logger.info(
                            f"Catch-up: IMAP check already produced inbound "
                            f"records today — waiting for next scheduled check"
                        )
                except Exception:
                    logger.exception("Catch-up check failed — skipping inbox check")

    # ── Follow-up catch-up: if past follow-up hour, run now ──
    # Weekday-only: 跟进引擎会发跟进邮件 (函数内另有 weekday 守卫兜底).
    now = datetime.now()
    if (
        config.FOLLOWUP_SCHEDULE_ENABLED
        and _is_weekday()
        and now.hour >= config.FOLLOWUP_CHECK_HOUR
    ):
        db_path = config.DB_PATH
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                try:
                    # Check if any follow-up was sent today
                    row = conn.execute(
                        "SELECT COUNT(*) FROM communications "
                        "WHERE direction='outbound' AND template_id LIKE 'followup_%' "
                        "AND sent_at >= date('now')"
                    ).fetchone()
                    has_followup_today = row[0] > 0 if row else False
                finally:
                    conn.close()

                if not has_followup_today:
                    logger.info(
                        f"Catch-up: follow-up time ({config.FOLLOWUP_CHECK_HOUR}:xx) "
                        f"passed, no follow-ups sent today — running now"
                    )
                    import threading
                    threading.Thread(
                        target=_run_scheduled_followups, daemon=True
                    ).start()
                else:
                    logger.info(
                        f"Catch-up: follow-ups already sent today "
                        f"({row[0]} today) — waiting for next scheduled run"
                    )
            except Exception:
                logger.exception("Catch-up check failed — skipping follow-up check")

    # ── Zoho sent-tracker catch-up: if past its hour and no report today, run now ──
    if config.ZOHO_SENT_ENABLED and now.hour >= config.ZOHO_SENT_HOUR:
        try:
            today_report = config.LOG_DIR / f"sent_tracker_{now:%Y-%m-%d}.md"
            if not today_report.exists():
                logger.info(
                    f"Catch-up: Zoho sent-tracker time ({config.ZOHO_SENT_HOUR}:xx) "
                    f"passed, no report today — running now"
                )
                import threading
                threading.Thread(
                    target=_run_scheduled_zoho_sent, daemon=True
                ).start()
            else:
                logger.info(
                    "Catch-up: Zoho sent report already exists today — "
                    "waiting for next scheduled run"
                )
        except Exception:
            logger.exception("Catch-up check failed — skipping Zoho sent tracker")

    # Optional immediate run
    if run_immediately:
        logger.info("Running immediate collection cycle...")
        _run_scheduled_collection()

    return _scheduler


def stop_scheduler(wait: bool = True):
    """Stop the background scheduler.

    Marks this process's still-running crawl tasks as failed BEFORE tearing
    down (see _mark_own_running_tasks_failed). If a long collection must not
    block shutdown, callers pass wait=False; the atexit hook is the backstop
    for exits that skip this function entirely.
    """
    global _scheduler
    _mark_own_running_tasks_failed("scheduler shutdown")
    if _scheduler is not None:
        logger.info("Shutting down scheduler...")
        _scheduler.shutdown(wait=wait)
        _scheduler = None
        logger.info("Scheduler stopped")


def get_scheduler_status() -> dict:
    """Get detailed scheduler status for API/health check.

    Includes: running state, next run time, time window config, segment info,
    recent task history, and latest health score.
    """
    global _scheduler, _next_run_info

    from collector.cli import SOURCE_REGISTRY

    # Get latest health score if available
    health_score = None
    avg_latency = None
    try:
        db_path = config.DB_PATH
        if db_path.exists():
            import sqlite3 as _sqlite3
            conn = _sqlite3.connect(str(db_path))
            conn.row_factory = _sqlite3.Row
            try:
                row = conn.execute(
                    """SELECT health_score, avg_latency_ms, error_count, total_requests
                       FROM collection_health
                       ORDER BY started_at DESC LIMIT 1"""
                ).fetchone()
                if row:
                    health_score = row["health_score"]
                    avg_latency = row["avg_latency_ms"]
            finally:
                conn.close()
    except Exception:
        pass

    jobs = []
    if _scheduler is not None:
        jobs = [
            {
                "id": j.id,
                "name": j.name,
                "next_run": str(j.next_run_time) if j.next_run_time else None,
            }
            for j in _scheduler.get_jobs()
        ]

    # Get today's email send count + all-time total
    email_sent_today = 0
    email_sent_total = 0
    email_remaining = config.DAILY_EMAIL_LIMIT
    try:
        db_path = config.DB_PATH
        if db_path.exists():
            import sqlite3 as _sqlite3
            from datetime import date as _date
            conn = _sqlite3.connect(str(db_path))
            conn.row_factory = _sqlite3.Row
            try:
                # Today's count from sending_quotas (primary counter)
                row = conn.execute(
                    "SELECT email_count FROM sending_quotas WHERE date = ?",
                    (_date.today().isoformat(),),
                ).fetchone()
                if row:
                    email_sent_today = row["email_count"]
                    email_remaining = max(0, config.DAILY_EMAIL_LIMIT - email_sent_today)
                # All-time total from communications table (ground truth)
                total_row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM communications WHERE status='sent'"
                ).fetchone()
                if total_row:
                    email_sent_total = total_row["cnt"]
            finally:
                conn.close()
    except Exception:
        pass

    # Get live email batch progress
    email_live = {}
    try:
        from emailer.batch import get_batch_status
        email_live = get_batch_status()
    except ImportError:
        pass

    return {
        "running": _scheduler is not None and _scheduler.running,
        "next_run": _next_run_info,
        "time_window": {
            "start_hour": config.SCHEDULE_TIME_WINDOW_START,
            "end_hour": config.SCHEDULE_TIME_WINDOW_END,
        },
        "segmentation": {
            "segment_size": config.COLLECTION_SEGMENT_SIZE,
        },
        "email_batch": {
            "enabled": config.EMAIL_SCHEDULE_ENABLED,
            "next_run": _email_next_run,
            "batch_size": config.EMAIL_BATCH_SIZE,
            "sent_today": email_sent_today,
            "sent_total": email_sent_total,
            "remaining": email_remaining,
            "live": email_live,
        },
        "zoho_batch": {
            "enabled": config.ZOHO_BATCH_ENABLED,
            "next_run": _zoho_batch_next_run,
            "quota": config.ZOHO_DAILY_LIMIT,
        },
        "jobs": jobs,
        "sources_registered": list(SOURCE_REGISTRY.keys()),
        "latest_health": {
            "score": health_score,
            "avg_latency_ms": avg_latency,
        },
        "recent_tasks": get_recent_tasks(days=1, limit=5),
    }
