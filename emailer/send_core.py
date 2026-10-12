"""Canonical outbound send orchestration — the single seam every real send
enters through before reaching the SMTP transport.

``send_batch`` remains the authoritative external entry for batch sends; this
module is the internal orchestration core it (and the legacy API, Streamlit,
follow-up engine and protected tools) all funnel through. The SendingGuard
decision is always evaluated and recorded here, and the SMTP transport is only
ever called from this module (via ``deliver`` / ``send_one`` /
``send_one_async``), so a BLOCK / DELAY / PAUSE_MAILBOX can never reach the
wire and can never be counted as an SMTP delivery failure.

Two-phase callers (e.g. ``send_batch``'s TOCTOU claim) use
``evaluate_send`` → (claim) → ``deliver``; callers with nothing in between use
``send_one`` / ``send_one_async`` directly. Either way the transport is only
reached from here, with a recorded ALLOW decision in hand.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional

from emailer.transport import AsyncEmailTransport, SyncEmailTransport
from emailer.inbound_readiness import normal_outbound_delivery_ready

from services.email_reputation.sending_guard import (
    GuardAction,
    GuardContext,
    GuardDecision,
    GuardDecisionRecorder,
    SendingGuard,
    StructuredLogGuardRecorder,
)
from services.email_reputation.delivery_authorization import (
    AuthorizationDecision,
    DeliveryAuthorizationGate,
    Purpose,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendIntent:
    """Typed, minimal send request shared by every real send entry point.

    Stable identifiers (lead / customer / campaign / stage / source / mailbox)
    are carried into the GuardContext so decisions are auditable per business
    object. ``subject`` / ``body_html`` are consumed only by the transport and
    only after an ALLOW decision — a two-phase caller (e.g. ``send_batch``'s
    TOCTOU claim) evaluates the guard first with content empty, then fills it
    through ``EvaluatedSend.with_rendered_content()`` immediately before
    ``deliver``.
    """

    email: str
    subject: str = ""
    body_html: str = ""
    to_name: Optional[str] = None
    lead_id: Optional[int] = None
    customer_id: Optional[int] = None
    template_id: Optional[str] = None
    stage: Optional[int] = None
    campaign: Optional[str] = None
    source: Optional[str] = None
    smtp_profile: str = "default"
    override_daily_quota: bool = False
    # P12 deliberately never infers a delivery purpose from source/campaign.
    # An unset purpose can never match a one-time operator authorization.
    purpose: Optional[Purpose] = None


@dataclass(frozen=True)
class SendOutcome:
    success: bool
    decision: GuardDecision
    message: str = ""
    message_id: Optional[str] = None
    guarded: bool = True
    dry_run: bool = False

    @property
    def blocked(self) -> bool:
        return self.decision.action is not GuardAction.ALLOW


_EVALUATION_CAPABILITY = object()


@dataclass(frozen=True)
class EvaluatedSend:
    """A recorded Guard decision bound to the intent it evaluated.

    Instances are produced only by :func:`evaluate_send`.  ``deliver`` accepts
    this capability rather than a freely constructible ``GuardDecision``, so a
    caller cannot manufacture ``ALLOW`` and reach SMTP without evaluation.
    Two-phase callers may fill in rendered content while preserving the
    recipient and all Guard-relevant identifiers.
    """

    intent: SendIntent
    decision: GuardDecision
    _capability: object

    def with_rendered_content(
        self,
        *,
        subject: str,
        body_html: str,
        template_id: Optional[str] = None,
    ) -> "EvaluatedSend":
        """Attach post-decision rendered content without changing identity."""
        return replace(
            self,
            intent=replace(
                self.intent,
                subject=subject,
                body_html=body_html,
                template_id=template_id,
            ),
        )


def _context(intent: SendIntent, now: datetime) -> GuardContext:
    return GuardContext(
        email=intent.email,
        mailbox=intent.smtp_profile,
        lead_id=intent.lead_id,
        customer_id=intent.customer_id,
        campaign=intent.campaign,
        stage=intent.stage,
        source=intent.source,
        requested_time=now.isoformat(),
        override_daily_quota=intent.override_daily_quota,
    )


def evaluate_send(
    intent: SendIntent,
    *,
    sending_guard: SendingGuard,
    recorder: Optional[GuardDecisionRecorder] = None,
    now: Optional[datetime] = None,
) -> EvaluatedSend:
    """Evaluate and record the Guard decision for a send request. No SMTP.

    Callers that must claim/allocate a lead between the decision and the
    transport use this, then ``deliver`` with the returned capability.
    """
    now = now or datetime.now(timezone.utc)
    context = _context(intent, now)
    decision = sending_guard.evaluate(context)
    (recorder or StructuredLogGuardRecorder()).record(context, decision)
    return EvaluatedSend(
        intent=intent, decision=decision, _capability=_EVALUATION_CAPABILITY
    )


def deliver(
    evaluated: EvaluatedSend,
    *,
    transport: Optional[SyncEmailTransport] = None,
    delivery_authorization_gate: Optional[DeliveryAuthorizationGate] = None,
) -> SendOutcome:
    """Transport seam — the only place the sync transport is reached.

    A capability from :func:`evaluate_send` is required. A non-ALLOW decision
    returns a blocked outcome without touching the wire. ``transport`` defaults to ``emailer.sender.send_email_sync``
    resolved through the module at call time, so existing test monkeypatches of
    ``emailer.sender.send_email_sync`` keep intercepting the default seam.
    """
    intent, decision = _validated_intent_and_decision(evaluated)
    if decision.action is not GuardAction.ALLOW:
        return SendOutcome(success=False, decision=decision, message=decision.message)
    if _global_dry_run_enabled():
        return simulate_delivery(evaluated)
    readiness_reason = _normal_outbound_readiness_reason(intent, decision)
    if readiness_reason is not None:
        return _normal_outbound_dry_run_outcome(intent, decision, readiness_reason)
    authorization_reason = _authorization_reason(
        intent, delivery_authorization_gate
    )
    if authorization_reason is not None:
        return _authorization_dry_run_outcome(intent, decision, authorization_reason)
    transport = transport or _sync_transport
    ok, msg, mid = transport(
        to_email=intent.email,
        subject=intent.subject,
        html_body=intent.body_html,
        to_name=intent.to_name,
        smtp_profile=intent.smtp_profile,
    )
    return SendOutcome(success=ok, decision=decision, message=msg, message_id=mid)


def simulate_delivery(evaluated: EvaluatedSend) -> SendOutcome:
    """Create P10's auditable simulation outcome without selecting SMTP.

    Two-phase callers invoke this before any irreversible local claim.  The
    same evaluated capability required by :func:`deliver` prevents a caller
    from manufacturing an ALLOW decision merely to create misleading audit
    output.
    """
    intent, decision = _validated_intent_and_decision(evaluated)
    if decision.action is not GuardAction.ALLOW:
        return SendOutcome(success=False, decision=decision, message=decision.message)
    return _dry_run_outcome(intent, decision)


def send_one(
    intent: SendIntent,
    *,
    sending_guard: SendingGuard,
    recorder: Optional[GuardDecisionRecorder] = None,
    transport: Optional[SyncEmailTransport] = None,
    delivery_authorization_gate: Optional[DeliveryAuthorizationGate] = None,
    now: Optional[datetime] = None,
) -> SendOutcome:
    """Decision + (ALLOW) sync transport — for callers with no claim in between."""
    evaluated = evaluate_send(
        intent, sending_guard=sending_guard, recorder=recorder, now=now
    )
    return deliver(
        evaluated,
        transport=transport,
        delivery_authorization_gate=delivery_authorization_gate,
    )


async def send_one_async(
    intent: SendIntent,
    *,
    sending_guard: SendingGuard,
    recorder: Optional[GuardDecisionRecorder] = None,
    transport: Optional[AsyncEmailTransport] = None,
    delivery_authorization_gate: Optional[DeliveryAuthorizationGate] = None,
    now: Optional[datetime] = None,
) -> SendOutcome:
    """Async twin of :func:`send_one` for the legacy async single-send paths.

    Uses ``emailer.sender.send_email`` (async) as the default transport,
    resolved through the module at call time so existing test monkeypatches of
    ``emailer.sender.send_email`` keep intercepting it.
    """
    evaluated = evaluate_send(
        intent, sending_guard=sending_guard, recorder=recorder, now=now
    )
    decision = evaluated.decision
    if decision.action is not GuardAction.ALLOW:
        return SendOutcome(success=False, decision=decision, message=decision.message)
    if _global_dry_run_enabled():
        return simulate_delivery(evaluated)
    readiness_reason = _normal_outbound_readiness_reason(evaluated.intent, decision)
    if readiness_reason is not None:
        return _normal_outbound_dry_run_outcome(
            evaluated.intent, decision, readiness_reason
        )
    authorization_reason = _authorization_reason(
        evaluated.intent, delivery_authorization_gate
    )
    if authorization_reason is not None:
        return _authorization_dry_run_outcome(
            evaluated.intent, decision, authorization_reason
        )
    transport = transport or _async_transport
    ok, msg, mid = await transport(
        to_email=intent.email,
        subject=intent.subject,
        html_body=intent.body_html,
        to_name=intent.to_name,
    )
    return SendOutcome(success=ok, decision=decision, message=msg, message_id=mid)


def global_dry_run_enabled() -> bool:
    """Expose P10's process-wide safety state to pre-transport callers.

    ``deliver`` and ``send_one_async`` enforce the gate themselves.  Batch
    callers also read it before claiming a lead, so a simulated outcome cannot
    leave a lead marked contacted while no message was sent.
    """
    return _global_dry_run_enabled()


def _global_dry_run_enabled() -> bool:
    import config

    return bool(config.EMAIL_DRY_RUN)


def _validated_intent_and_decision(
    evaluated: EvaluatedSend,
) -> tuple[SendIntent, GuardDecision]:
    if not isinstance(evaluated, EvaluatedSend) or (
        evaluated._capability is not _EVALUATION_CAPABILITY
    ):
        raise TypeError("delivery requires an EvaluatedSend from evaluate_send()")
    return evaluated.intent, evaluated.decision


def _dry_run_outcome(intent: SendIntent, decision: GuardDecision) -> SendOutcome:
    """Emit a metadata-only P10 audit record and simulate delivery."""
    logger.info(
        "email_global_dry_run %s",
        json.dumps(
            {
                "email": intent.email,
                "mailbox": intent.smtp_profile,
                "lead_id": intent.lead_id,
                "customer_id": intent.customer_id,
                "campaign": intent.campaign,
                "stage": intent.stage,
                "source": intent.source,
                "reason_code": decision.reason_code.value,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    return SendOutcome(
        success=False,
        decision=decision,
        message="global dry-run: SMTP delivery was simulated",
        guarded=True,
        dry_run=True,
    )


def _authorization_reason(
    intent: SendIntent,
    gate: Optional[DeliveryAuthorizationGate],
) -> Optional[str]:
    """Enforce P12 test authorization unless P13 normal delivery is enabled.

    This helper is called only after the Guard ALLOW decision and after P10's
    global lock check.  P13's explicit operator control restores the normal
    guarded send paths; it never bypasses SendingGuard.  Otherwise an
    unavailable or failing P12 authorization store falls back to dry-run; it
    can never become a transport bypass.
    """
    if _normal_outbound_delivery_enabled():
        logger.info(
            "email_normal_outbound_delivery %s",
            json.dumps(
                {
                    "email": intent.email,
                    "mailbox": intent.smtp_profile,
                    "lead_id": intent.lead_id,
                    "source": intent.source,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        return None
    if gate is None:
        return "delivery authorization gate is required"
    try:
        attempt = gate.authorize_and_consume(
            lead_id=intent.lead_id,
            email=intent.email,
            mailbox=intent.smtp_profile,
            purpose=intent.purpose,
        )
    except Exception:
        logger.exception("delivery authorization gate failed closed")
        return "delivery authorization could not be verified"
    if attempt.decision is not AuthorizationDecision.ALLOW:
        return f"delivery authorization denied: {attempt.reason}"
    return None


def _normal_outbound_delivery_enabled() -> bool:
    """Read the explicit P13 operator control without exposing configuration."""
    import config

    return bool(getattr(config, "OUTBOUND_DELIVERY_ENABLED", False))


def _normal_outbound_readiness_reason(
    intent: SendIntent, decision: GuardDecision
) -> Optional[str]:
    """Require a live asynchronous-bounce monitor for P13 normal delivery."""
    if not _normal_outbound_delivery_enabled():
        return None
    if intent.smtp_profile != "default":
        return (
            "normal outbound delivery is currently limited to the default "
            "mailbox until this profile has scheduled DSN monitoring"
        )
    ready, reason = normal_outbound_delivery_ready(intent.smtp_profile)
    if ready:
        return None
    logger.warning(
        "email_normal_outbound_blocked %s",
        json.dumps(
            {
                "email": intent.email,
                "mailbox": intent.smtp_profile,
                "lead_id": intent.lead_id,
                "source": intent.source,
                "reason": reason,
                "reason_code": decision.reason_code.value,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    return reason


def _normal_outbound_dry_run_outcome(
    intent: SendIntent, decision: GuardDecision, reason: str
) -> SendOutcome:
    """Fail closed before SMTP when P13's inbound DSN monitor is unavailable."""
    return SendOutcome(
        success=False,
        decision=decision,
        message=f"normal outbound dry-run: {reason}",
        guarded=True,
        dry_run=True,
    )


def _authorization_dry_run_outcome(
    intent: SendIntent, decision: GuardDecision, reason: str
) -> SendOutcome:
    """Audit an unlocked-but-unauthorized P12 attempt without SMTP."""
    logger.info(
        "email_delivery_authorization_dry_run %s",
        json.dumps(
            {
                "email": intent.email,
                "mailbox": intent.smtp_profile,
                "lead_id": intent.lead_id,
                "customer_id": intent.customer_id,
                "campaign": intent.campaign,
                "stage": intent.stage,
                "source": intent.source,
                "purpose": intent.purpose.value if intent.purpose else None,
                "reason": reason,
                "reason_code": decision.reason_code.value,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    return SendOutcome(
        success=False,
        decision=decision,
        message=f"delivery authorization dry-run: {reason}",
        guarded=True,
        dry_run=True,
    )


def _sync_transport(
    to_email: str,
    subject: str,
    html_body: str,
    to_name: Optional[str] = None,
    smtp_profile: str = "default",
) -> tuple[bool, str, Optional[str]]:
    from emailer import sender

    return sender.send_email_sync(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        to_name=to_name,
        smtp_profile=smtp_profile,
    )


async def _async_transport(
    to_email: str,
    subject: str,
    html_body: str,
    to_name: Optional[str] = None,
) -> tuple[bool, str, Optional[str]]:
    from emailer import sender

    return await sender.send_email(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        to_name=to_name,
    )
