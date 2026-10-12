"""
土耳其经销商开发信工具（Zoho）——收件人与定向文案仅从本地私有配置读取。

用法:
  python tools/send_turkey_dealers.py --dry-run
  python tools/send_turkey_dealers.py --config data/private/turkey_dealers.json --dry-run
  python tools/send_turkey_dealers.py --force

先 dry-run 核对配置再真实发送。--force 仅保留原有每日额度 override，
其他 SendingGuard / P12 / 全局 dry-run 保护仍生效。未配置时安全退出。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

FTA_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FTA_ROOT))

import config  # noqa: E402
from dataclasses import replace  # noqa: E402
from emailer.rotation import AntiSpamGuard  # noqa: E402
from emailer.send_core import SendIntent, deliver, evaluate_send  # noqa: E402
from services.email_reputation.diagnostic_safety import sanitize_diagnostic  # noqa: E402
from services.email_reputation.sending_guard import (  # noqa: E402
    GuardAction,
    GuardReasonCode,
    build_sending_guard,
)

DEFAULT_CAMPAIGN_PATH = FTA_ROOT / "data" / "private" / "turkey_dealers.json"


def _load_campaign(path: Path) -> dict:
    """Load operator-owned recipients and content without exposing file values."""
    try:
        campaign = json.loads(path.read_text(encoding="utf-8"))
        recipients = campaign["recipients"]
        subject = campaign["subject"]
        template = campaign["body_template"]
        if not isinstance(recipients, list) or not recipients:
            raise ValueError
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError
        if not isinstance(template, str) or not template.strip():
            raise ValueError
        for recipient in recipients:
            if not isinstance(recipient, dict) or any(
                not isinstance(recipient.get(key), str) or not recipient[key].strip()
                for key in ("to", "company", "focus")
            ):
                raise ValueError
            # Validate template placeholders before opening a DB or sending.
            template.format(company=recipient["company"], focus=recipient["focus"], sig="")
        return campaign
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        raise ValueError("Campaign configuration is missing or invalid; no mail attempted.") from None


def _sig_text() -> str:
    return "\n".join(
        [
            config.SENDER_NAME,
            "Trade Assistant",
            config.SENDER_COMPANY,
            config.SENDER_ADDRESS,
            config.SENDER_CERTS,
            f"WhatsApp: {config.SENDER_WHATSAPP}",
            f"Tel: {config.SENDER_PHONE}",
            f"E-mail: {config.SENDER_EMAIL}",
            f"Web: {config.SENDER_WEBSITE}",
        ]
    )


def _sig_html() -> str:
    return "<br>".join(
        [
            config.SENDER_NAME,
            "Trade Assistant",
            config.SENDER_COMPANY,
            config.SENDER_ADDRESS,
            config.SENDER_CERTS,
            f"WhatsApp: {config.SENDER_WHATSAPP}",
            f"Tel: {config.SENDER_PHONE}",
            f"E-mail: {config.SENDER_EMAIL}",
            f"Web: {config.SENDER_WEBSITE}",
        ]
    )


def _body_html(r: dict, body_template: str) -> str:
    sig = _sig_html()
    body = body_template.format(company=r["company"], focus=r["focus"], sig=sig)
    parts = [p.strip() for p in body.split("\n\n")]
    return "<p>" + "</p><p>".join(p.replace("\n", "<br>") for p in parts) + "</p>"


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — 非 tty/不支持时跳过
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()  # Windows GBK 控制台无法打印土耳其语字符(ğ/ş/ı) → UTF-8
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="只打印内容 + 配额检查, 不发")
    ap.add_argument("--force", action="store_true",
                    help="突破今日 Zoho 配额(Daily limit reached 时仍发; 黑名单/MX 仍校验)")
    ap.add_argument("--config", type=Path, default=DEFAULT_CAMPAIGN_PATH,
                    help="本机私有收件人与定向文案 JSON（禁止提交）")
    args = ap.parse_args(argv)
    try:
        campaign = _load_campaign(args.config)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    recipients = campaign["recipients"]
    subject = campaign["subject"]
    body_template = campaign["body_template"]

    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    guard = AntiSpamGuard(conn, mailbox="zoho")
    sending_guard = build_sending_guard(
        conn, mailbox="zoho", anti_spam_guard=guard
    )

    results: list[str] = []
    for r in recipients:
        body = _body_html(r, body_template)
        intent = SendIntent(
            email=r["to"],
            subject=subject,
            body_html=body,
            to_name=r["company"],
            smtp_profile="zoho",
            source="tool:send_turkey_dealers",
        )
        # Evaluate the decision WITHOUT the operator override first, so the
        # printed verdict shows what would normally happen; --force only
        # bypasses the daily-quota DELAY (suppression/MX/domain still block).
        base_evaluated = evaluate_send(
            intent,
            sending_guard=sending_guard,
        )
        base_decision = base_evaluated.decision
        force_ok = (
            args.force
            and base_decision.reason_code is GuardReasonCode.DAILY_QUOTA_EXCEEDED
        )
        print("=" * 70)
        print(f"TO:      {r['to']}")
        print(f"SUBJECT: {subject}")
        print(
            f"GUARD:   {base_decision.action.value} "
            f"({base_decision.reason_code.value})  force_ok={force_ok}"
        )
        print("-" * 70)
        print(body_template.format(company=r["company"], focus=r["focus"], sig=_sig_text()))
        if base_decision.action is not GuardAction.ALLOW and not force_ok:
            results.append(f"[SKIP] {r['to']}: {base_decision.message}")
            continue
        if args.dry_run:
            results.append(f"[DRY-RUN] {r['to']}: would send (force_ok={force_ok})")
            continue

        # Only when forcing do we re-evaluate with the override so the audit
        # trail records the explicit operator bypass (DAILY_QUOTA_OVERRIDDEN).
        # The override lifts ONLY the daily cap — suppression / MX / per-domain
        # tier still apply, so the forced decision can still come back BLOCK.
        evaluated = (
            evaluate_send(
                replace(intent, override_daily_quota=True),
                sending_guard=sending_guard,
            )
            if force_ok
            else base_evaluated
        )
        decision = evaluated.decision
        if decision.action is not GuardAction.ALLOW:
            # A guard decision is never an SMTP failure — no wire contact was
            # attempted, so nothing to record and nothing to suppress.
            results.append(f"[SKIP] {r['to']}: {decision.message}")
            continue
        outcome = deliver(evaluated)
        if outcome.dry_run:
            # P10 safety outcome: this is not a provider failure and must not
            # consume the independent Zoho quota.
            results.append(f"[GLOBAL DRY-RUN] {r['to']}: {outcome.message}")
        elif outcome.success:
            guard.record_send(r["to"])
            results.append(
                f"[SENT] {r['to']}: {outcome.message} ({outcome.message_id})"
            )
        else:
            guard.record_smtp_failure(r["to"], outcome.message, provider="zoho")
            msg = sanitize_diagnostic(outcome.message)
            results.append(f"[FAIL] {r['to']}: {msg}")

    conn.close()
    print("=" * 70)
    print("SUMMARY:")
    for line in results:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
