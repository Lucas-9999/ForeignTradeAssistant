"""
FTA 定期清理 / 健康辅助 — shared by the Saturday self-check job and manual runs.

Functions:
    clean_logs(retention_days, dry_run)       -> int  # dated + rotated log files
    cleanup_db_retention(retention_days, dry_run) -> int  # collection_health rows
    db_size_report()                          -> str  # fta.db / wa.db / logs sizes
    scan_zombie_processes()                   -> str  # python processes vs stale tasks

Usage:
    python tools/cleanup.py                    # dry-run: 报告将清理什么
    python tools/cleanup.py --apply            # 真正清理
    python tools/cleanup.py --report           # 仅输出 DB/日志健康报告
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config  # noqa: E402

# The active rotating log must never be deleted by clean_logs.
ACTIVE_LOGS = {"fta.log"}
LOG_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")
STALE_TASK_HOURS = 6  # status='running' older than this = zombie (matches scheduler)


def _fmt(n: float) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _file_age_days(path: Path) -> int:
    """Prefer the date embedded in the filename (e.g. app_restart_2026-08-05.log);
    fall back to file mtime for undated files (audit_report.md, fta.log.N)."""
    m = LOG_DATE_RE.search(path.name)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            return (date.today() - d).days
        except ValueError:
            pass
    try:
        return (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).days
    except OSError:
        return 0


def clean_logs(retention_days: int = 30, dry_run: bool = True) -> int:
    """Delete log files older than retention_days (never the active fta.log).

    Covers dated logs (app_restart_*/balanced_send_*/zoho_batch_*/sent_tracker_*/
    saturday_report_*/health_report_*), rotated fta.log.N, and undated reports.
    Returns the number of files that would be / were deleted.
    """
    if not config.LOG_DIR.exists():
        return 0
    candidates = [
        p for p in config.LOG_DIR.iterdir()
        if p.is_file() and p.name not in ACTIVE_LOGS
    ]
    doomed = [p for p in candidates if _file_age_days(p) >= retention_days]
    if not doomed:
        return 0
    tag = "[DRY RUN]" if dry_run else "[APPLY]"
    for p in sorted(doomed):
        print(f"{tag} 删除日志: {p.name} (age={_file_age_days(p)}d)")
        if not dry_run:
            try:
                p.unlink()
            except OSError as e:
                print(f"  [ERROR] 无法删除 {p.name}: {e}")
    return len(doomed)


def cleanup_db_retention(retention_days: int = 30, dry_run: bool = True) -> int:
    """Purge collection_health rows older than retention_days.

    Wires up the previously-dead config.MONITOR_RETENTION_DAYS. Returns the
    number of rows that would be / were deleted.
    """
    db_path = config.DB_PATH
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        # Table may not exist yet on a fresh DB.
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_health'"
        ).fetchone()
        if not has_table:
            return 0
        # SQLite datetime('now', '-N days') — N must be a literal-ish string.
        span = f"-{retention_days} days"
        count = conn.execute(
            "SELECT COUNT(*) FROM collection_health WHERE started_at < datetime('now', ?)",
            (span,),
        ).fetchone()[0]
        if count and not dry_run:
            conn.execute(
                "DELETE FROM collection_health WHERE started_at < datetime('now', ?)",
                (span,),
            )
            conn.commit()
        tag = "[DRY RUN]" if dry_run else "[APPLY]"
        if count:
            print(f"{tag} collection_health 删除 {count} 行 (>{retention_days}d)")
        return count
    finally:
        conn.close()


def db_size_report() -> str:
    """Sizes of fta.db / wa.db / logs dir + key table row counts."""
    lines = []

    def _row_count(db: Path, table: str) -> int:
        if not db.exists():
            return -1
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                r = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                return r[0] if r else 0
            finally:
                conn.close()
        except Exception:
            return -1

    lines.append(f"- fta.db: {_fmt(os.path.getsize(config.DB_PATH)) if config.DB_PATH.exists() else '缺失'}")
    wa_db = Path(os.getenv("FTA_WA_DB_PATH", str(BASE_DIR.parent / "WhatsApp Assistant" / "data" / "wa.db")))
    lines.append(f"- wa.db:  {_fmt(os.path.getsize(wa_db)) if wa_db.exists() else '缺失'}")
    if config.LOG_DIR.exists():
        total = sum(p.stat().st_size for p in config.LOG_DIR.iterdir() if p.is_file())
        n = len([p for p in config.LOG_DIR.iterdir() if p.is_file()])
        lines.append(f"- logs/:  {_fmt(total)} ({n} 个文件)")

    if config.DB_PATH.exists():
        for table in ("leads", "communications", "scraping_tasks", "collection_health"):
            n = _row_count(config.DB_PATH, table)
            lines.append(f"- 表 {table}: {n:,} 行" if n >= 0 else f"- 表 {table}: (不可读)")
    return "\n".join(lines)


def scan_zombie_processes() -> str:
    """Cross-check running python processes against stale DB tasks.

    Windows: `tasklist` (no psutil dependency). Reports:
      - python.exe process count + PIDs
      - scraping_tasks left 'running' > STALE_TASK_HOURS (likely orphans)
    Returns a human-readable summary (empty-safe).
    """
    out: list[str] = []

    # 1) Running python processes
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=60,
        )
        # CSV columns: Image Name, PID, Session Name, Session#, Mem Usage.
        pids = []
        for line in (r.stdout or "").splitlines():
            if not line.strip():
                continue
            fields = line.strip().strip('"').split('","')
            if len(fields) >= 2 and fields[1].isdigit():
                pids.append(fields[1])
        out.append(f"python.exe 进程: {len(pids)} 个 PID={', '.join(pids[:20])}" if pids
                   else "python.exe 进程: 0 (或 tasklist 不可用)")
    except Exception as e:
        out.append(f"tasklist 失败: {type(e).__name__}: {e}")

    # 2) Stale running DB tasks (likely orphaned by killed processes)
    if config.DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(config.DB_PATH))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, source, started_at FROM scraping_tasks "
                    "WHERE status='running' AND started_at < datetime('now', ?)",
                    (f"-{STALE_TASK_HOURS} hours",),
                ).fetchall()
                if rows:
                    out.append(
                        "僵尸采集任务(>{}h running): ".format(STALE_TASK_HOURS)
                        + "; ".join(f"#{r['id']} {r['source']} 自 {r['started_at']}" for r in rows)
                    )
                else:
                    out.append("僵尸采集任务: (无)")
            finally:
                conn.close()
        except Exception as e:
            out.append(f"僵尸任务查询失败: {type(e).__name__}: {e}")

    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="FTA 定期清理 / 健康报告")
    ap.add_argument("--apply", action="store_true", help="真正清理(默认 dry-run)")
    ap.add_argument("--report", action="store_true", help="仅输出 DB/日志健康报告")
    args = ap.parse_args()

    if args.report:
        print(db_size_report())
        return

    dry_run = not args.apply
    print(f"清理模式: {'DRY-RUN(不删除)' if dry_run else 'APPLY(真正删除)'}")
    print(f"- 日志保留 {config.LOG_RETENTION_DAYS} 天")
    print(f"- collection_health 保留 {config.MONITOR_RETENTION_DAYS} 天\n")

    clean_logs(retention_days=config.LOG_RETENTION_DAYS, dry_run=dry_run)
    cleanup_db_retention(retention_days=config.MONITOR_RETENTION_DAYS, dry_run=dry_run)
    print("\n" + db_size_report())


if __name__ == "__main__":
    main()
