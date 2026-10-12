"""
Merge existing leads duplicated by 寻客星 buyer_id (one-time cleanup).

Leads carrying the same platform-stable buyer_id (source_detail JSON) are
duplicates of the same upstream record. This script keeps MIN(id) as the
authoritative row and marks the rest duplicate_of + status='dead', migrating
their communications.lead_id to the kept row — same semantics as
crm/manager.py::merge_duplicates. Nothing is physically deleted.

Usage:
    python tools/merge_buyer_duplicates.py            # dry-run: 报告组/行数
    python tools/merge_buyer_duplicates.py --apply    # 合并(自动备份 DB 后执行)

Run with the app stopped (SQLite WAL + large transaction). After apply,
restart the app and re-run dry-run to confirm 0 groups remain.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "fta.db"


def _backup_db(con: sqlite3.Connection) -> Path:
    """Snapshot the DB via the SQLite backup API (WAL-safe)."""
    backup_path = BASE_DIR / "data" / f"fta.db.bak_buyer_merge_{datetime.now():%Y%m%d_%H%M%S}"
    dst = sqlite3.connect(str(backup_path))
    try:
        con.backup(dst)
    finally:
        dst.close()
    print(f"[备份] {backup_path}")
    return backup_path


def main() -> int:
    ap = argparse.ArgumentParser(description="合并寻客星 buyer_id 重复线索")
    ap.add_argument("--apply", action="store_true", help="真正合并(默认 dry-run)")
    ap.add_argument("--source", default="xunkexing")
    ap.add_argument("--chunk", type=int, default=5000)
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[ERROR] DB not found: {DB_PATH}")
        return 1

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    groups = cur.execute(
        """SELECT CAST(json_extract(source_detail, '$.buyer_id') AS TEXT) AS bid,
                  MIN(id) AS keep_id, COUNT(*) AS cnt, GROUP_CONCAT(id) AS all_ids
           FROM leads
           WHERE source = ?
             AND json_extract(source_detail, '$.buyer_id') IS NOT NULL
             AND duplicate_of IS NULL
           GROUP BY bid HAVING COUNT(*) > 1
           ORDER BY cnt DESC""",
        (args.source,),
    ).fetchall()

    merge_rows = sum(len(g["all_ids"].split(",")) - 1 for g in groups)
    tag = "DRY-RUN" if not args.apply else "APPLY"
    print(f"[{tag}] buyer_id 重复组: {len(groups)}, 待合并行: {merge_rows}")
    for g in groups[:10]:
        ids = [int(i) for i in g["all_ids"].split(",")]
        print(f"  buyer_id={g['bid']}: keep=#{g['keep_id']}, merge={len(ids) - 1}行")
    if len(groups) > 10:
        print(f"  ... 共 {len(groups)} 组")

    if not args.apply or not groups:
        con.close()
        return 0

    _backup_db(con)

    pairs = [
        (g["keep_id"], int(i))
        for g in groups
        for i in g["all_ids"].split(",")
        if int(i) != g["keep_id"]
    ]

    done = 0
    for i in range(0, len(pairs), args.chunk):
        chunk_pairs = pairs[i:i + args.chunk]
        cur.executemany(
            "UPDATE communications SET lead_id=? WHERE lead_id=?",
            [(k, m) for k, m in chunk_pairs],
        )
        cur.executemany(
            "UPDATE leads SET duplicate_of=?, status='dead', updated_at=datetime('now') "
            "WHERE id=?",
            [(k, m) for k, m in chunk_pairs],
        )
        con.commit()
        done += len(chunk_pairs)
        print(f"  [APPLY] 已合并 {done}/{len(pairs)} 行")

    print(f"[APPLIED] 合并 {len(pairs)} 行 (duplicate_of 标记 + communications 迁移)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
