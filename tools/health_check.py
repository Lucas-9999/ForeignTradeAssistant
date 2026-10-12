"""
FTA 一键运行健康度检查 (read-only) → logs/health_report_YYYY-MM-DD.md

Covers: git 状态/仓库大小 · 数据源审计 · 线索去重核查(buyer_id/email) ·
近期采集错误 · 卡死任务 · 日志/DB 大小 · 回归测试 · 残留进程 · IMAP 诊断.

Usage:
    python tools/health_check.py               # 全量(含回归测试, 可能较慢)
    python tools/health_check.py --skip-tests  # 跳过 pytest(快速)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config  # noqa: E402

REPORT_DIR = config.LOG_DIR
STALE_TASK_HOURS = 6


def _sh(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return (r.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"(命令失败: {type(e).__name__}: {e})"


def _ro_conn():
    return sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)


def _dedup_check() -> str:
    """只读: buyer_id / email 重复 + 无邮箱占比."""
    if not config.DB_PATH.exists():
        return "(DB 缺失)"
    con = _ro_conn()
    con.row_factory = sqlite3.Row
    try:
        cur = con.cursor()
        groups, rows = cur.execute(
            """SELECT COUNT(*) g, COALESCE(SUM(c), 0) r FROM (
                SELECT COUNT(*) c FROM leads
                WHERE json_extract(source_detail, '$.buyer_id') IS NOT NULL
                  AND duplicate_of IS NULL
                GROUP BY CAST(json_extract(source_detail, '$.buyer_id') AS TEXT)
                HAVING COUNT(*) > 1)"""
        ).fetchone()
        email_dup = cur.execute(
            """SELECT COUNT(*) FROM (
                SELECT LOWER(email) FROM leads
                WHERE email IS NOT NULL AND TRIM(email) <> ''
                GROUP BY LOWER(email) HAVING COUNT(*) > 1)"""
        ).fetchone()[0]
        total = cur.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        no_email = cur.execute(
            "SELECT COUNT(*) FROM leads WHERE email IS NULL OR TRIM(email) = ''"
        ).fetchone()[0]
        return (
            f"- buyer_id 重复组: {groups:,} (含重复行 {rows:,})\n"
            f"- email 重复: {email_dup:,}\n"
            f"- 线索总数: {total:,}, 无邮箱: {no_email:,} ({no_email / total:.0%})"
        )
    finally:
        con.close()


def _recent_errors() -> str:
    if not config.DB_PATH.exists():
        return "(DB 缺失)"
    con = _ro_conn()
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT id, source, status, leads_found, leads_new, error_message
               FROM scraping_tasks WHERE error_message IS NOT NULL
               ORDER BY id DESC LIMIT 15"""
        ).fetchall()
        if not rows:
            return "(近 15 条任务均无错误)"
        return "\n".join(
            f"- #{r['id']} {r['source']} [{r['status']}] found={r['leads_found']} "
            f"new={r['leads_new']} — {(r['error_message'] or '')[:200]}"
            for r in rows
        )
    finally:
        con.close()


def _stale_tasks() -> str:
    if not config.DB_PATH.exists():
        return "(DB 缺失)"
    con = _ro_conn()
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, source, started_at FROM scraping_tasks "
            "WHERE status='running' AND started_at < datetime('now', ?)",
            (f"-{STALE_TASK_HOURS} hours",),
        ).fetchall()
        return "\n".join(f"- #{r['id']} {r['source']} 自 {r['started_at']}" for r in rows) \
            or "(无)"
    finally:
        con.close()


def _dns_audit(
    domain: str,
    dkim_selector: str | None = None,
    *,
    sending_ip: str | None = None,
    smtp_host: str | None = None,
    smtp_port: int = 465,
    persist: bool = False,
) -> str:
    """Read-only Phase 5 DNS/authentication snapshot for an operator domain."""
    try:
        from services.email_reputation.dns_audit import DnsAuditor
        from services.email_reputation.health_scoring import (
            persist_domain_audit,
            score_domain_audit,
        )

        audit = DnsAuditor().audit(
            domain,
            dkim_selector=dkim_selector,
            sending_ip=sending_ip,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
        )
        recommendation = score_domain_audit(audit)
        result = (
            f"- 域名: {audit.domain}\n"
            f"- MX: {audit.mx_status.value}; A/AAAA: {audit.a_status.value}\n"
            f"- SPF: {audit.spf_status.value}; DKIM: {audit.dkim_status.value}; "
            f"DMARC: {audit.dmarc_status.value}\n"
            f"- PTR: {audit.ptr_status.value}; TLS: {audit.tls_status.value}; "
            f"认证汇总: {audit.authentication_status.value}\n"
            f"- 影子建议: score={recommendation.health_score:.1f}, "
            f"status={recommendation.status.value} ({recommendation.reason})\n"
            "- 本检查不修改发送策略。"
        )
        if not persist:
            return result + "\n- 未传 --persist：不写入 health 表。"
        from services.email_reputation.repository import EmailReputationRepository

        with sqlite3.connect(config.DB_PATH) as conn:
            snapshot, event = persist_domain_audit(
                EmailReputationRepository(conn),
                audit,
                checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        event_label = "已追加变化事件" if event else "快照无变化，未追加事件"
        return result + (
            f"\n- 已按显式 --persist 写入 domain_health："
            f"{snapshot.status.value} / {snapshot.health_score:.1f}；{event_label}。"
        )
    except Exception as e:  # noqa: BLE001
        return f"[ERROR] {type(e).__name__}: {e}"


def _run_checks(
    skip_tests: bool, *, dns_domain: str | None = None, dkim_selector: str | None = None,
    sending_ip: str | None = None, smtp_host: str | None = None, smtp_port: int = 465,
    persist_dns: bool = False,
) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []

    sections.append(("git 状态 / 仓库大小", _sh(["git", "status", "--short"]) + "\n\n" +
                     _sh(["git", "count-objects", "-vH"])))

    try:
        from tools.audit import audit, _md
        sections.append(("数据源审计 (tools/audit)", _md(audit())))
    except Exception as e:  # noqa: BLE001
        sections.append(("数据源审计", f"[ERROR] {type(e).__name__}: {e}"))

    sections.append(("线索去重核查 (buyer_id / email / 无邮箱)", _dedup_check()))
    sections.append(("近期采集错误 (top 15)", _recent_errors()))
    sections.append((f"卡死任务 (> {STALE_TASK_HOURS}h running)", _stale_tasks()))
    if dns_domain:
        sections.append((
            "DNS / 发信认证审计（P5 影子模式）",
            _dns_audit(
                dns_domain,
                dkim_selector,
                sending_ip=sending_ip,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                persist=persist_dns,
            ),
        ))

    try:
        from tools.cleanup import db_size_report
        sections.append(("日志 / DB 大小", db_size_report()))
    except Exception as e:  # noqa: BLE001
        sections.append(("日志 / DB 大小", f"[ERROR] {type(e).__name__}: {e}"))

    if skip_tests:
        sections.append(("回归测试", "(已通过 --skip-tests 跳过)"))
    else:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=short"],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=1800,
            )
            tail = "\n".join((r.stdout or "").splitlines()[-20:])
            sections.append((f"回归测试 (pytest exit={r.returncode})",
                             f"```\n{tail}\n```"))
        except subprocess.TimeoutExpired:
            sections.append(("回归测试", "[TIMEOUT] pytest >30min, 请手动运行确认"))
        except Exception as e:  # noqa: BLE001
            sections.append(("回归测试", f"[ERROR] {type(e).__name__}: {e}"))

    try:
        from tools.cleanup import scan_zombie_processes
        sections.append(("残留进程", scan_zombie_processes()))
    except Exception as e:  # noqa: BLE001
        sections.append(("残留进程", f"[ERROR] {type(e).__name__}: {e}"))

    try:
        from engine.diagnostics import run_diagnostics
        d = run_diagnostics(check_inbox=True, check_sent=True)
        sections.append(("IMAP 诊断",
                         f"```json\n{json.dumps(d, ensure_ascii=False, indent=2, default=str)}\n```"))
    except Exception as e:  # noqa: BLE001
        sections.append(("IMAP 诊断", f"[ERROR] {type(e).__name__}: {e}"))

    return sections


def main() -> int:
    ap = argparse.ArgumentParser(description="FTA 一键运行健康度检查")
    ap.add_argument("--skip-tests", action="store_true", help="跳过 pytest")
    ap.add_argument("--dns-domain", help="只读审计指定发信域名（P5 影子模式）")
    ap.add_argument("--dkim-selector", help="可选 DKIM selector；未提供时保持 UNKNOWN")
    ap.add_argument("--sending-ip", help="可选发信公网 IP，用于 PTR 审计")
    ap.add_argument("--smtp-host", help="可选 SMTP 主机，用于无认证 implicit-TLS 检查")
    ap.add_argument("--smtp-port", type=int, default=465, help="implicit-TLS SMTP 端口（默认 465）")
    ap.add_argument(
        "--persist", action="store_true",
        help="配合 --dns-domain，写入已有 domain_health 快照；不改变发送策略",
    )
    args = ap.parse_args()

    print("FTA 运行健康度检查进行中... (read-only)")
    sections = _run_checks(
        skip_tests=args.skip_tests,
        dns_domain=args.dns_domain,
        dkim_selector=args.dkim_selector,
        sending_ip=args.sending_ip,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        persist_dns=args.persist,
    )

    lines = [f"# FTA 健康检查报告 — {date.today():%Y-%m-%d}",
             f"\n生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"]
    for title, body in sections:
        lines.append(f"\n## {title}\n{body}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"health_report_{date.today():%Y-%m-%d}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[written] {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
