"""Phase 5 DNS/authentication audit, deliberately read-only and send-free.

The auditor accepts a resolver seam so tests never contact public DNS. Runtime
DNS failures are represented as ``UNKNOWN`` rather than ``FAIL``: a resolver
outage is not evidence that a sender domain is misconfigured. DKIM remains
``UNKNOWN`` until an operator supplies a non-secret selector; guessing one is
not a valid authentication check.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import socket
import ssl
from typing import Callable, Optional, Protocol, Sequence

from services.email_reputation.models import DnsStatus


class DnsResolver(Protocol):
    def resolve(self, name: str, record_type: str) -> Sequence[object]: ...


@dataclass(frozen=True)
class DomainDnsAudit:
    domain: str
    sending_ip: Optional[str]
    mx_status: DnsStatus
    spf_status: DnsStatus
    dkim_status: DnsStatus
    dmarc_status: DnsStatus
    a_status: DnsStatus
    ptr_status: DnsStatus
    tls_status: DnsStatus
    authentication_status: DnsStatus


class DnsAuditor:
    """Evaluate public DNS records without mutating DNS, DB or send policy."""

    def __init__(
        self,
        resolver: Optional[DnsResolver] = None,
        *,
        tls_checker: Optional[Callable[[str, int], bool]] = None,
    ):
        self._resolver = resolver or default_resolver()
        self._tls_checker = tls_checker or _check_implicit_tls

    def audit(
        self,
        domain: str,
        *,
        dkim_selector: Optional[str] = None,
        sending_ip: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_port: int = 465,
    ) -> DomainDnsAudit:
        normalized = _normalize_domain(domain)
        normalized_ip = _normalize_sending_ip(sending_ip)
        mx = self._presence(normalized, "MX", missing=DnsStatus.FAIL)
        a = self._address_status(normalized)
        spf = self._txt_prefix(normalized, "v=spf1")
        dmarc = self._txt_prefix(f"_dmarc.{normalized}", "v=dmarc1")
        dkim = (
            self._txt_prefix(
                f"{dkim_selector.strip()}._domainkey.{normalized}", "v=dkim1"
            )
            if dkim_selector and dkim_selector.strip()
            else DnsStatus.UNKNOWN
        )
        authentication = _authentication_status(spf, dkim, dmarc)
        return DomainDnsAudit(
            domain=normalized,
            sending_ip=normalized_ip,
            mx_status=mx,
            spf_status=spf,
            dkim_status=dkim,
            dmarc_status=dmarc,
            a_status=a,
            ptr_status=self._ptr_status(normalized_ip),
            tls_status=self._tls_status(smtp_host, smtp_port),
            authentication_status=authentication,
        )

    def _presence(self, name: str, record_type: str, *, missing: DnsStatus) -> DnsStatus:
        records, available = self._records(name, record_type)
        if not available:
            return DnsStatus.UNKNOWN
        return DnsStatus.PASS if records else missing

    def _address_status(self, domain: str) -> DnsStatus:
        a_records, a_available = self._records(domain, "A")
        aaaa_records, aaaa_available = self._records(domain, "AAAA")
        if a_records or aaaa_records:
            return DnsStatus.PASS
        if not a_available and not aaaa_available:
            return DnsStatus.UNKNOWN
        if not a_available or not aaaa_available:
            return DnsStatus.UNKNOWN
        return DnsStatus.FAIL

    def _txt_prefix(self, name: str, prefix: str) -> DnsStatus:
        records, available = self._records(name, "TXT")
        if not available:
            return DnsStatus.UNKNOWN
        normalized_prefix = prefix.lower()
        if any(_record_text(record).lower().startswith(normalized_prefix) for record in records):
            return DnsStatus.PASS
        return DnsStatus.WARNING

    def _ptr_status(self, sending_ip: Optional[str]) -> DnsStatus:
        if not sending_ip:
            return DnsStatus.UNKNOWN
        try:
            reverse_name = ipaddress.ip_address(sending_ip.strip()).reverse_pointer
        except ValueError:
            return DnsStatus.UNKNOWN
        return self._presence(reverse_name, "PTR", missing=DnsStatus.FAIL)

    def _tls_status(self, smtp_host: Optional[str], smtp_port: int) -> DnsStatus:
        if not smtp_host:
            return DnsStatus.UNKNOWN
        try:
            return (
                DnsStatus.PASS
                if self._tls_checker(smtp_host.strip(), int(smtp_port))
                else DnsStatus.FAIL
            )
        except ssl.SSLError:
            # The server was reached but could not complete a trusted TLS
            # handshake (certificate/protocol mismatch): this is a definite
            # readiness failure, unlike a timeout or network outage.
            return DnsStatus.FAIL
        except Exception:
            return DnsStatus.UNKNOWN

    def _records(self, name: str, record_type: str) -> tuple[list[object], bool]:
        try:
            return list(self._resolver.resolve(name, record_type)), True
        except Exception as exc:  # Resolver libraries use provider-specific errors.
            if is_missing_record_error(exc):
                return [], True
            return [], False


def default_resolver() -> DnsResolver:
    """Create the bounded default resolver for DNS-readiness modules.

    This public seam is shared by P5 and P9.  It performs no database or SMTP
    work and keeps bounded timeouts so diagnostics degrade to ``UNKNOWN``.
    """
    try:
        import dns.resolver
    except ImportError as exc:  # Defensive: report UNKNOWN until dependency is installed.
        raise RuntimeError("dnspython is required for DNS auditing") from exc
    resolver = dns.resolver.Resolver()
    # Health checks must degrade to UNKNOWN promptly when the local network or
    # resolver is unavailable; never let five sequential lookups stall app
    # diagnostics for a minute.
    resolver.timeout = 1.0
    resolver.lifetime = 2.0
    return resolver


def _check_implicit_tls(host: str, port: int) -> bool:
    """Probe an implicit-TLS SMTP endpoint without credentials or SMTP commands."""
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=2.0) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host):
            return True


def _normalize_domain(domain: str) -> str:
    normalized = domain.strip().rstrip(".").lower()
    if not normalized or "." not in normalized or any(ch.isspace() for ch in normalized):
        raise ValueError("domain must be a DNS name")
    return normalized


def _normalize_sending_ip(value: Optional[str]) -> Optional[str]:
    if not value or not value.strip():
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def _record_text(record: object) -> str:
    if hasattr(record, "strings"):
        strings = getattr(record, "strings")
        return "".join(
            value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
            for value in strings
        )
    return str(record).strip('"')


def is_missing_record_error(exc: Exception) -> bool:
    """Classify authoritative missing-record errors across DNS libraries."""
    name = type(exc).__name__.lower()
    return name in {"noanswer", "nxdomain"} or "not found" in str(exc).lower()


# Backward-compatible aliases for existing internal callers and P5 tests.
_default_resolver = default_resolver
_is_missing_record_error = is_missing_record_error


def _authentication_status(
    spf: DnsStatus, dkim: DnsStatus, dmarc: DnsStatus
) -> DnsStatus:
    statuses = (spf, dkim, dmarc)
    if DnsStatus.FAIL in statuses:
        return DnsStatus.FAIL
    if DnsStatus.WARNING in statuses:
        return DnsStatus.WARNING
    if DnsStatus.UNKNOWN in statuses:
        return DnsStatus.UNKNOWN
    return DnsStatus.PASS
