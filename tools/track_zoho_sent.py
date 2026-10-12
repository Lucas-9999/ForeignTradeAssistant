"""
Zoho Sent-Follow-Up Tracker CLI — detect the user's manual Zoho follow-ups.

Usage:
    python tools/track_zoho_sent.py                    # scan today, record + report
    python tools/track_zoho_sent.py --days 2           # include yesterday too (首跑建议)
    python tools/track_zoho_sent.py --report-only      # dry-run: scan+classify, no DB writes
    python tools/track_zoho_sent.py --no-report        # record but skip the md report
    python tools/track_zoho_sent.py --limit 20         # cap scanned sent emails

Zoho is personal communication only — this tool NEVER sends email and does
NOT touch the NetEase batch/follow-up pipeline.

Prereq: .env has FTA_ZOHO_IMAP_USERNAME / FTA_ZOHO_IMAP_PASSWORD. If 2FA is
on, use a Zoho app-specific password (Zoho Mail → Settings → Security →
App Passwords), not the account password.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# Allow `python tools/track_zoho_sent.py` to import project packages.
sys.path.insert(0, str(BASE_DIR))

# Windows console is GBK by default — reconfigure to UTF-8 so the Chinese
# report prints cleanly (report files are always written as UTF-8).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import config  # noqa: E402
from crm import db as crm_db  # noqa: E402
from engine.sent_tracker import ZohoSentTracker  # noqa: E402

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format=config.LOG_FORMAT,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Zoho sent-follow-up tracker")
    parser.add_argument(
        "--days", type=int, default=None,
        help=f"scan window in days (default: config.ZOHO_SENT_WINDOW_DAYS={config.ZOHO_SENT_WINDOW_DAYS})",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="scan + classify + report only, NO database writes (safe dry-run)",
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="record into CRM but skip writing the markdown report",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help=f"max sent emails to scan (default: config.ZOHO_SENT_MAX_EMAILS={config.ZOHO_SENT_MAX_EMAILS})",
    )
    args = parser.parse_args()

    if args.limit is not None:
        config.ZOHO_SENT_MAX_EMAILS = args.limit

    if not config.DB_PATH.exists():
        print(f"Database not found at {config.DB_PATH}. Run the app first to initialize it.")
        return 1

    conn = crm_db.get_connection(config.DB_PATH)
    tracker = ZohoSentTracker(conn, log_dir=config.LOG_DIR)
    try:
        result = tracker.run(
            days=args.days,
            report_only=args.report_only,
            write_report=not args.no_report,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
