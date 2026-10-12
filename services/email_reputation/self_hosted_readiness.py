"""Phase 9 self-hosted SMTP readiness checks, deliberately send-free.

The module evaluates evidence supplied by P5 DNS auditing plus an explicit
open-relay security result.  It never opens an SMTP connection, never handles
credentials and never selects a transport; a future operator-approved phase
must provide any active probe or production transport.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Optional, Sequence

from services.email_reputation.dns_audit import (
    DnsResolver,
    DomainDnsAudit,
    default_resolver,
    is_missing_record_error,
)
from services.email_reputation.models import DnsStatus


@dataclass(frozen=True)
class FcrdnsResult:
    """Forward-confirmed reverse-DNS result for one declared sending IP."""

    sending_ip: Optional[str]
    ptr_hostname: Optional[str]
    status: DnsStatus


class FcrdnsAuditor:
    """Check PTR → A/AAAA round-trip using a supplied DNS resolver only."""

    def __init__(self, resolver: Optional[DnsResolver] = None):
        self._resolver = resolver or default_resolver()

    def audit(self, sending_ip: Optional[str]) -> FcrdnsResult:
        try:
            normalized_ip = str(ipaddress.ip_address((sending_ip or "").strip()))
        except ValueError:
            return FcrdnsResult(sending_ip=None, ptr_hostname=None, status=DnsStatus.UNKNOWN)

        reverse_name = ipaddress.ip_address(normalized_ip).reverse_pointer
        ptr_records, ptr_available = self._records(reverse_name, "PTR")
        if not ptr_available:
            return FcrdnsResult(normalized_ip, None, DnsStatus.UNKNOWN)
        if not ptr_records:
            return FcrdnsResult(normalized_ip, None, DnsStatus.FAIL)

        saw_unavailable = False
        first_hostname: Optional[str] = None
        for record in ptr_records:
            hostname = str(record).strip().rstrip(".").lower()
            if not hostname:
                continue
            first_hostname = first_hostname or hostname
            addresses, available = self._forward_addresses(hostname)
            if not available:
                saw_unavailable = True
                continue
            if normalized_ip in addresses:
                return FcrdnsResult(normalized_ip, hostname, DnsStatus.PASS)
        return FcrdnsResult(
            normalized_ip,
            first_hostname,
            DnsStatus.UNKNOWN if saw_unavailable else DnsStatus.FAIL,
        )

    def _forward_addresses(self, hostname: str) -> tuple[set[str], bool]:
        addresses: set[str] = set()
        all_available = True
        for record_type in ("A", "AAAA"):
            records, query_available = self._records(hostname, record_type)
            all_available = all_available and query_available
            for record in records:
                try:
                    addresses.add(str(ipaddress.ip_address(str(record).strip())))
                except ValueError:
                    continue
        return addresses, all_available

    def _records(self, name: str, record_type: str) -> tuple[list[object], bool]:
        try:
            return list(self._resolver.resolve(name, record_type)), True
        except Exception as exc:  # DNS implementations use provider-specific errors.
            if is_missing_record_error(exc):
                return [], True
            return [], False


@dataclass(frozen=True)
class SelfHostedReadinessReport:
    """Readiness verdict only; ``ready`` never enables a transport."""

    ready: bool
    blockers: tuple[str, ...]


def evaluate_self_hosted_readiness(
    audit: DomainDnsAudit,
    *,
    fcrdns_status: DnsStatus,
    open_relay_status: DnsStatus,
) -> SelfHostedReadinessReport:
    """Require PASS evidence for auth, TLS, PTR/FCrDNS and closed relay.

    ``UNKNOWN`` is a blocker: absence of evidence is not evidence that a
    self-hosted SMTP server is safe to use.
    """
    checks = (
        ("mx", audit.mx_status),
        ("spf", audit.spf_status),
        ("dkim", audit.dkim_status),
        ("dmarc", audit.dmarc_status),
        ("a", audit.a_status),
        ("ptr", audit.ptr_status),
        ("tls", audit.tls_status),
        ("authentication", audit.authentication_status),
        ("fcrdns", fcrdns_status),
        ("open_relay", open_relay_status),
    )
    blockers = tuple(f"{name}={status.value}" for name, status in checks if status is not DnsStatus.PASS)
    return SelfHostedReadinessReport(ready=not blockers, blockers=blockers)
