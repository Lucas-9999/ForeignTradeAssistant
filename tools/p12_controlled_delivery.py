"""P12 one-time controlled delivery command.

This is intentionally the only operator entry that can open P10's process-
local dry-run lock.  It uses a fixed, non-marketing test message and still
requires Guard ALLOW plus an exact, unexpired P11 authorization before SMTP.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config
from crm.db import ensure_schema_current, get_connection
from crm.manager import CommunicationManager, LeadManager
from crm.models import Communication
from emailer.rotation import AntiSpamGuard
from emailer.send_core import SendIntent, send_one
from emailer.transport import SyncEmailTransport
from services.email_reputation.delivery_authorization import (
    DeliveryAuthorizationGate,
    DeliveryAuthorizationRepository,
    Purpose,
)
from services.email_reputation.sending_guard import build_sending_guard
from utils.cli_logging import configure_cli_logging


logger = logging.getLogger(__name__)


SUBJECT = "FTA controlled delivery test"
BODY_HTML = (
    "<p>This is a one-time controlled delivery test from the Foreign Trade "
    "Assistant. No reply or action is required.</p>"
)
ALLOWED_MAILBOXES = frozenset({"default", "zoho"})


@dataclass(frozen=True)
class ControlledDeliveryResult:
    success: bool
    dry_run: bool
    message: str
    message_id: Optional[str] = None


def _record_success(conn, *, lead_id: int, message_id: Optional[str]) -> None:
    """Write normal delivery state only after the SMTP transport succeeded."""
    CommunicationManager(conn).log(
        Communication(
            lead_id=lead_id,
            channel="email",
            direction="outbound",
            subject=SUBJECT,
            body=BODY_HTML,
            status="sent",
            message_id=message_id,
        )
    )
    LeadManager(conn).mark_contacted(lead_id)


def run_controlled_delivery(
    *,
    lead_id: int,
    mailbox: str,
    purpose: Purpose,
    confirmation: str,
    transport: Optional[SyncEmailTransport] = None,
) -> ControlledDeliveryResult:
    """Perform one exact P12 delivery, or return a no-side-effect dry outcome."""
    if purpose is not Purpose.CONTROLLED_TEST:
        return ControlledDeliveryResult(False, True, "only controlled_test is permitted")
    if mailbox not in ALLOWED_MAILBOXES:
        return ControlledDeliveryResult(False, True, "mailbox is not approved for controlled delivery")
    if confirmation != f"SEND-{lead_id}":
        return ControlledDeliveryResult(False, True, "confirmation did not match the lead")

    ensure_schema_current(config.DB_PATH)
    conn = get_connection(config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT email, contact_name FROM leads WHERE id=?", (lead_id,)
        ).fetchone()
        if row is None or not row["email"]:
            return ControlledDeliveryResult(False, True, "lead does not exist or has no email")

        anti_spam = AntiSpamGuard(conn, mailbox=mailbox)
        sending_guard = build_sending_guard(
            conn, mailbox=mailbox, anti_spam_guard=anti_spam
        )
        authorization_gate = DeliveryAuthorizationGate(
            DeliveryAuthorizationRepository(conn),
            now=lambda: datetime.now(timezone.utc),
        )
        intent = SendIntent(
            email=str(row["email"]),
            subject=SUBJECT,
            body_html=BODY_HTML,
            to_name=row["contact_name"],
            lead_id=lead_id,
            source="p12_controlled_delivery",
            smtp_profile=mailbox,
            purpose=purpose,
        )

        # The unlock is process-local, scoped to this command, and protected
        # by send_core's mandatory authorization gate.  It is restored even
        # if guard, authorization, or SMTP fails.
        original_dry_run = config.EMAIL_DRY_RUN
        config.EMAIL_DRY_RUN = False
        try:
            try:
                outcome = send_one(
                    intent,
                    sending_guard=sending_guard,
                    delivery_authorization_gate=authorization_gate,
                    transport=transport,
                )
            except Exception:
                logger.exception(
                    "controlled delivery raised an exception; check authorization status before retry"
                )
                return ControlledDeliveryResult(
                    False,
                    False,
                    "controlled delivery failed; check authorization status before retry",
                )
        finally:
            config.EMAIL_DRY_RUN = original_dry_run

        if outcome.dry_run or not outcome.success:
            return ControlledDeliveryResult(
                False, outcome.dry_run, outcome.message, outcome.message_id
            )
        try:
            anti_spam.record_send(intent.email)
            _record_success(conn, lead_id=lead_id, message_id=outcome.message_id)
        except Exception:
            logger.exception(
                "controlled delivery sent but local recording failed; manual reconciliation required"
            )
            return ControlledDeliveryResult(
                True,
                False,
                "sent, but local recording failed; manual reconciliation required",
                outcome.message_id,
            )
        return ControlledDeliveryResult(True, False, outcome.message, outcome.message_id)
    finally:
        conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P12 精确单次受控投递")
    parser.add_argument("--lead-id", type=int, required=True)
    parser.add_argument("--mailbox", choices=sorted(ALLOWED_MAILBOXES), required=True)
    parser.add_argument("--purpose", choices=[Purpose.CONTROLLED_TEST.value], required=True)
    parser.add_argument("--confirm", required=True, help="must be exactly SEND-<lead-id>")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    configure_cli_logging()
    args = _parser().parse_args(argv)
    result = run_controlled_delivery(
        lead_id=args.lead_id,
        mailbox=args.mailbox,
        purpose=Purpose(args.purpose),
        confirmation=args.confirm,
    )
    print(result.message)
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(main())
