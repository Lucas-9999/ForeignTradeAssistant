"""
FTA Data-Source Health Audit — read-only diagnostic for the 4 known issues:
  1. 寻客星数据源枯竭趋势 (XunKeXing saturation)
  2. 获取数据与数据库重叠率 (overlap with existing DB data)
  3. Facebook 线索率低 (Facebook yield)
  4. 浏览器搜索/行业目录站成功率 (industry sites yield)
  + 数据质量 (email coverage) + 卡死任务 (stale running tasks) + token 有效期

Usage:
    python tools/audit.py            # print report to terminal
    python tools/audit.py --md       # also write logs/audit_report.md

Read-only: never writes to the database.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# Allow `python tools/audit.py` to import project packages (utils/…).
sys.path.insert(0, str(BASE_DIR))
DB_PATH = BASE_DIR / "data" / "fta.db"
TOKEN_PATH = BASE_DIR / "data" / "xkx_token.json"
INDUSTRY_SITES_PATH = BASE_DIR / "data" / "industry_sites.json"
FB_TARGETS_PATH = BASE_DIR / "data" / "fb_target_groups.json"

STALE_TASK_HOURS = 12  # status='running' older than this = stale


def _q(cur: sqlite3.Cursor, sql: str, params: tuple = ()):
    cur.execute(sql, params)
    return cur.fetchall()


def audit() -> dict:
    if not DB_PATH.exists():
        print(f"[ERROR] DB not found: {DB_PATH}")
        sys.exit(1)

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    report: dict = {}

    # ── 1. 枯竭趋势: 最近 14 天每日新增 + 最近 8 次采集 ───────────────────
    rows = _q(cur, """
        SELECT date(discovered_at) AS d, COUNT(*) AS n
        FROM leads WHERE discovered_at >= date('now', '-14 days')
        GROUP BY d ORDER BY d
    """)
    report["daily_new_14d"] = [{"date": r["d"], "count": r["n"]} for r in rows]

    rows = _q(cur, """
        SELECT id, source, status, leads_found, leads_new,
               (leads_found - leads_new) AS dupes,
               started_at, error_message
        FROM scraping_tasks WHERE source='xunkexing'
        ORDER BY id DESC LIMIT 8
    """)
    report["recent_xkx_runs"] = [dict(r) for r in rows]

    # ── 2. 重叠率: 最近采集 created/valid ─────────────────────────────────
    rows = _q(cur, """
        SELECT source, COUNT(*) AS runs,
               SUM(leads_found) AS found, SUM(leads_new) AS new_leads,
               SUM(CASE WHEN leads_found > 0 THEN 1 ELSE 0 END) AS nonzero_runs
        FROM scraping_tasks GROUP BY source
    """)
    report["task_summary"] = [dict(r) for r in rows]

    # ── 3. Facebook 全部任务 ─────────────────────────────────────────────
    rows = _q(cur, """
        SELECT status, COUNT(*) AS n, SUM(leads_found) AS found, SUM(leads_new) AS new_leads
        FROM scraping_tasks WHERE source='facebook' GROUP BY status
    """)
    report["facebook_summary"] = [dict(r) for r in rows]
    rows = _q(cur, """
        SELECT started_at, status, leads_found, leads_new, error_message
        FROM scraping_tasks WHERE source='facebook' ORDER BY id DESC LIMIT 12
    """)
    report["facebook_recent"] = [dict(r) for r in rows]

    # ── 4. 行业目录站 ────────────────────────────────────────────────────
    if INDUSTRY_SITES_PATH.exists():
        sites = json.loads(INDUSTRY_SITES_PATH.read_text(encoding="utf-8"))
        report["industry_sites"] = [
            {"name": s.get("name"), "enabled": s.get("enabled", False),
             "disabled_reason": s.get("_disabled_reason")}
            for s in sites.get("sites", [])
        ]
    rows = _q(cur, """
        SELECT status, COUNT(*) AS n, SUM(leads_found) AS found, SUM(leads_new) AS new_leads
        FROM scraping_tasks WHERE source='industry' GROUP BY status
    """)
    report["industry_tasks"] = [dict(r) for r in rows]

    # ── 5. 数据质量 ──────────────────────────────────────────────────────
    report["leads_total"] = _q(cur, "SELECT COUNT(*) AS n FROM leads")[0]["n"]
    rows = _q(cur, """
        SELECT source, COUNT(*) AS n,
               SUM(CASE WHEN email IS NOT NULL AND TRIM(email) <> '' THEN 1 ELSE 0 END) AS with_email
        FROM leads GROUP BY source
    """)
    report["email_coverage"] = [dict(r) for r in rows]
    rows = _q(cur, """
        SELECT status, COUNT(*) AS n FROM leads GROUP BY status
    """)
    report["status_dist"] = [dict(r) for r in rows]
    report["no_email"] = _q(cur, """
        SELECT COUNT(*) AS n FROM leads
        WHERE email IS NULL OR TRIM(email) = ''
    """)[0]["n"]

    # 无邮箱线索按国家分布 (top 10)
    rows = _q(cur, """
        SELECT COALESCE(NULLIF(TRIM(country), ''), '(unknown)') AS c, COUNT(*) AS n
        FROM leads WHERE email IS NULL OR TRIM(email) = ''
        GROUP BY c ORDER BY n DESC LIMIT 10
    """)
    report["no_email_by_country"] = [{"country": r["c"], "count": r["n"]} for r in rows]

    # 有邮箱线索的顶级域名 (top 15)
    rows = _q(cur, """
        SELECT SUBSTR(LOWER(TRIM(email)), INSTR(TRIM(email), '@') + 1) AS d, COUNT(*) AS n
        FROM leads
        WHERE email IS NOT NULL AND TRIM(email) <> '' AND INSTR(TRIM(email), '@') > 0
        GROUP BY d ORDER BY n DESC LIMIT 15
    """)
    report["top_domains"] = [{"domain": r["d"], "count": r["n"]} for r in rows]

    # ── 6. 卡死任务 ──────────────────────────────────────────────────────
    rows = _q(cur, """
        SELECT id, source, started_at, completed_at
        FROM scraping_tasks
        WHERE status='running'
          AND started_at < datetime('now', ?)
    """, (f"-{STALE_TASK_HOURS} hours",))
    report["stale_running"] = [dict(r) for r in rows]

    # ── 7. 寻客星 token 有效期 ───────────────────────────────────────────
    report["token_status"] = "missing"
    if TOKEN_PATH.exists():
        try:
            tok = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
            exp = tok.get("expires_at")
            report["token_status"] = f"expires={exp}"
        except Exception as e:  # noqa: BLE001
            report["token_status"] = f"unreadable: {e}"

    con.close()
    return report


def _mx_sample(sample_limit: int = 200) -> dict:
    """MX-check a sample of distinct domains from 'new' leads with email.

    Slow (DNS lookups) — opt-in via `--mx`. Reports dead domains that the
    send-time filter would otherwise re-check on every batch.
    """
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = _q(cur, """
        SELECT DISTINCT SUBSTR(LOWER(TRIM(email)), INSTR(TRIM(email), '@') + 1) AS d
        FROM leads
        WHERE status='new' AND email IS NOT NULL AND TRIM(email) <> ''
          AND INSTR(TRIM(email), '@') > 0
        LIMIT ?
    """, (sample_limit,))
    con.close()

    from utils.email_verify import has_mx_record
    dead: list[str] = []
    for i, r in enumerate(rows, 1):
        domain = r["d"]
        ok = has_mx_record(domain)
        if not ok:
            dead.append(domain)
        if i % 50 == 0:
            print(f"  MX check {i}/{len(rows)}...")
    return {"sampled": len(rows), "dead_domains": dead}


def _md(report: dict) -> str:
    L = []
    L.append(f"# FTA 数据源健康自检报告\n\n生成时间: {datetime.now():%Y-%m-%d %H:%M}\n")
    L.append(f"## 线索总量: {report['leads_total']:,}\n")

    L.append("\n## 1. 寻客星枯竭趋势 — 最近 14 天每日新增\n")
    if report["daily_new_14d"]:
        L.append("| 日期 | 新增 |")
        L.append("|------|------|")
        for r in report["daily_new_14d"]:
            L.append(f"| {r['date']} | {r['count']} |")
    else:
        L.append("(无)")

    L.append("\n## 2. 最近 8 次寻客星采集(重叠率)\n")
    L.append("| id | 时间 | found | new | 重复(估算) | 状态 |")
    L.append("|----|------|-------|-----|-----------|------|")
    for r in report["recent_xkx_runs"]:
        rate = f"{r['leads_new'] / r['leads_found']:.1%}" if r["leads_found"] else "-"
        L.append(
            f"| {r['id']} | {r['started_at']} | {r['leads_found']:,} | "
            f"{r['leads_new']} | {r['dupes']:,} | {r['status']} (新率 {rate}) |"
        )

    L.append("\n## 3. Facebook 线索率\n")
    for r in report["facebook_summary"]:
        L.append(f"- {r['status']}: {r['n']} 次运行, 共 found={r['found']}, new={r['new_leads']}")
    L.append("\n最近运行:")
    for r in report["facebook_recent"][:6]:
        L.append(f"  - {r['started_at']} {r['status']} found={r['leads_found']} new={r['leads_new']} {r['error_message'] or ''}")

    L.append("\n## 4. 行业目录站(浏览器搜索)\n")
    for s in report.get("industry_sites", []):
        why = f" — {s['disabled_reason']}" if not s["enabled"] and s["disabled_reason"] else ""
        L.append(f"- {s['name']}: {'启用' if s['enabled'] else '禁用'}{why}")
    for r in report.get("industry_tasks", []):
        L.append(f"- industry 任务: {r['status']} {r['n']} 次, found={r['found']}, new={r['new_leads']}")

    L.append("\n## 5. 数据质量\n")
    L.append("| 来源 | 总数 | 有邮箱 | 覆盖率 |")
    L.append("|------|------|--------|--------|")
    for r in report["email_coverage"]:
        rate = f"{r['with_email'] / r['n']:.0%}" if r["n"] else "-"
        L.append(f"| {r['source']} | {r['n']:,} | {r['with_email']:,} | {rate} |")
    L.append(f"\n无邮箱线索: {report['no_email']:,}")
    L.append("\n无邮箱线索 Top 10 国家:")
    for r in report["no_email_by_country"]:
        L.append(f"- {r['country']}: {r['count']:,}")
    L.append("\n有邮箱线索 Top 10 域名:")
    for r in report["top_domains"][:10]:
        L.append(f"- {r['domain']}: {r['count']:,}")
    L.append("\n状态分布: " + ", ".join(f"{r['status']}={r['n']:,}" for r in report["status_dist"]))

    L.append("\n## 6. 卡死任务(>12h running)\n")
    if report["stale_running"]:
        for r in report["stale_running"]:
            L.append(f"- id={r['id']} {r['source']} 自 {r['started_at']} 未结束")
    else:
        L.append("(无)")

    L.append(f"\n## 7. 寻客星 token: {report['token_status']}\n")
    return "\n".join(L)


def main() -> None:
    report = audit()
    text = _md(report)

    if "--mx" in sys.argv:
        print("\n── MX 域名抽样(慢, DNS 查询)──")
        sample = _mx_sample(sample_limit=200)
        print(f"  抽样 {sample['sampled']} 个域名, 死域 {len(sample['dead_domains'])} 个:")
        for d in sample["dead_domains"][:50]:
            print(f"    - {d}")
        text += "\n## MX 抽样\n"
        text += f"抽样 {sample['sampled']} 域名, 死域 {len(sample['dead_domains'])} 个:\n"
        for d in sample["dead_domains"][:50]:
            text += f"- {d}\n"

    print(text)
    if "--md" in sys.argv:
        out = BASE_DIR / "logs" / "audit_report.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
