"""
Mark 'new' leads whose email domain has no MX record as dead.

Why: the send-time filter re-checks dead domains (extinct TLDs like .yu,
defunct ISPs) on every daily batch, wasting quota selection. Marking them
dead once lets the batch skip them permanently.

Usage:
    python tools/quality_cleanup.py                # dry-run (report only)
    python tools/quality_cleanup.py --apply        # actually mark dead
    python tools/quality_cleanup.py --limit 2000   # bound DNS lookups

This only touches leads with status='new'. dead/contacted/replied are
never modified. Idempotent — re-runs skip already-marked leads.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# Allow `python tools/quality_cleanup.py` to import project packages.
sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "data" / "fta.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="actually mark dead-domain leads as dead (default: dry-run)")
    parser.add_argument("--limit", type=int, default=2000,
                        help="max distinct domains to MX-check (DNS is slow)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"[ERROR] DB not found: {DB_PATH}")
        return 1

    from utils.email_verify import has_mx_record

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Distinct domains of 'new' leads with email.
    cur.execute("""
        SELECT DISTINCT LOWER(SUBSTR(TRIM(email), INSTR(TRIM(email), '@') + 1)) AS d
        FROM leads
        WHERE status='new' AND email IS NOT NULL AND TRIM(email) <> ''
          AND INSTR(TRIM(email), '@') > 0
        ORDER BY d
    """)
    domains = [r["d"] for r in cur.fetchall()]

    print(f"候选域名: {len(domains)} (检查上限 {args.limit})")
    dead_domains: list[str] = []
    for i, d in enumerate(domains[:args.limit], 1):
        if not has_mx_record(d):
            dead_domains.append(d)
        if i % 200 == 0:
            print(f"  MX 检查 {i}/{min(len(domains), args.limit)}...")
    print(f"\n无 MX 域名: {len(dead_domains)}/{len(domains[:args.limit])}")

    if not dead_domains:
        print("无死域名 — 无需清理")
        con.close()
        return 0

    # Count affected leads per dead domain.
    placeholders = ",".join("?" for _ in dead_domains)
    cur.execute(f"""
        SELECT COUNT(*) AS n FROM leads
        WHERE status='new'
          AND LOWER(SUBSTR(TRIM(email), INSTR(TRIM(email), '@') + 1)) IN ({placeholders})
    """, dead_domains)
    affected = cur.fetchone()["n"]
    print(f"受影响 'new' 线索: {affected} 条")

    # Show top dead domains
    for d in dead_domains[:20]:
        cur.execute("""
            SELECT COUNT(*) AS n FROM leads
            WHERE status='new'
              AND LOWER(SUBSTR(TRIM(email), INSTR(TRIM(email), '@') + 1)) = ?
        """, (d,))
        print(f"  - {d}: {cur.fetchone()['n']} 条")

    if args.apply:
        cur.execute(f"""
            UPDATE leads SET status='dead', updated_at=CURRENT_TIMESTAMP
            WHERE status='new'
              AND LOWER(SUBSTR(TRIM(email), INSTR(TRIM(email), '@') + 1)) IN ({placeholders})
        """, dead_domains)
        con.commit()
        print(f"\n[APPLIED] 已标记 {cur.rowcount} 条死域线索为 dead")
    else:
        print("\n[dry-run] 未修改数据库 — 加 --apply 生效")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
