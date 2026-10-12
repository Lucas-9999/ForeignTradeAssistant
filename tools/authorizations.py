"""P11 operator allowlist management — never sends email or opens SMTP."""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config
from crm.db import ensure_schema_current, get_connection
from services.email_reputation.delivery_authorization import (
    AuthorizationStatus, DeliveryAuthorizationRepository, Purpose,
)
from utils.cli_logging import configure_cli_logging


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--expires-at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("--expires-at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P11 精确单次投递授权管理（不发送邮件）")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="列出最近的授权记录")
    listing.add_argument("--status", choices=[status.value for status in AuthorizationStatus])
    listing.add_argument("--limit", type=int, default=50)
    create = commands.add_parser("create", help="创建一条精确且会过期的授权")
    create.add_argument("--lead-id", type=int, required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--mailbox", required=True)
    create.add_argument("--purpose", required=True, choices=[purpose.value for purpose in Purpose])
    create.add_argument("--expires-at", required=True)
    create.add_argument("--approval-note", required=True)
    revoke = commands.add_parser("revoke", help="撤销一条 active 授权（保留审计记录）")
    revoke.add_argument("--id", type=int, required=True)
    return parser


def _lead_email(conn, lead_id: int) -> str:
    row = conn.execute("SELECT email FROM leads WHERE id=?", (lead_id,)).fetchone()
    if row is None or not row[0]:
        raise ValueError("lead does not exist or has no email")
    return str(row[0])


def main(argv: list[str] | None = None) -> int:
    configure_cli_logging()
    args = _parser().parse_args(argv)
    try:
        ensure_schema_current(config.DB_PATH)
        conn = get_connection(config.DB_PATH)
        repo = DeliveryAuthorizationRepository(conn)
        try:
            if args.command == "list":
                status = AuthorizationStatus(args.status) if args.status else None
                for item in repo.list(limit=args.limit, status=status):
                    print(f"#{item.id} {item.status.value} lead={item.lead_id} email={item.email} "
                          f"mailbox={item.mailbox} purpose={item.purpose.value} expires_at={item.expires_at}")
                return 0
            now = datetime.now(timezone.utc)
            if args.command == "create":
                requested = repo.normalize_email(args.email)
                if repo.normalize_email(_lead_email(conn, args.lead_id)) != requested:
                    raise ValueError("email does not match the lead's canonical email")
                item = repo.create(lead_id=args.lead_id, email=requested, mailbox=args.mailbox,
                    purpose=Purpose(args.purpose), expires_at=_parse_timestamp(args.expires_at),
                    created_by=getpass.getuser(), approval_note=args.approval_note, now=now)
                print(f"created authorization #{item.id} (P10 dry-run remains locked)")
                return 0
            if repo.revoke(args.id, revoked_by=getpass.getuser(), now=now):
                print(f"revoked authorization #{args.id}")
                return 0
            print(f"authorization #{args.id} was not active", file=sys.stderr)
            return 2
        finally:
            conn.close()
    except (LookupError, ValueError, RuntimeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
