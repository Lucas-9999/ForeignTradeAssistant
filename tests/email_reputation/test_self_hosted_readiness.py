"""P9 self-hosted SMTP readiness tests — no network or SMTP is contacted."""

from __future__ import annotations

from services.email_reputation.dns_audit import DomainDnsAudit
from services.email_reputation.models import DnsStatus
from services.email_reputation.self_hosted_readiness import (
    FcrdnsAuditor,
    evaluate_self_hosted_readiness,
)
from emailer.transport import self_hosted_transport_enabled


class _Resolver:
    def __init__(self, answers):
        self.answers = answers

    def resolve(self, name, record_type):
        answer = self.answers.get((name, record_type), [])
        if isinstance(answer, Exception):
            raise answer
        return answer


def _ready_dns_audit() -> DomainDnsAudit:
    return DomainDnsAudit(
        domain="example.com",
        sending_ip="192.0.2.10",
        mx_status=DnsStatus.PASS,
        spf_status=DnsStatus.PASS,
        dkim_status=DnsStatus.PASS,
        dmarc_status=DnsStatus.PASS,
        a_status=DnsStatus.PASS,
        ptr_status=DnsStatus.PASS,
        tls_status=DnsStatus.PASS,
        authentication_status=DnsStatus.PASS,
    )


def test_fcrdns_passes_only_when_ptr_hostname_resolves_back_to_sending_ip():
    resolver = _Resolver({
        ("10.2.0.192.in-addr.arpa", "PTR"): ["mail.example.com."],
        ("mail.example.com", "A"): ["192.0.2.10"],
        ("mail.example.com", "AAAA"): [],
    })

    result = FcrdnsAuditor(resolver).audit("192.0.2.10")

    assert result.status is DnsStatus.PASS
    assert result.ptr_hostname == "mail.example.com"


def test_fcrdns_fails_when_ptr_hostname_does_not_resolve_back_to_sending_ip():
    resolver = _Resolver({
        ("10.2.0.192.in-addr.arpa", "PTR"): ["mail.example.com."],
        ("mail.example.com", "A"): ["192.0.2.99"],
        ("mail.example.com", "AAAA"): [],
    })

    result = FcrdnsAuditor(resolver).audit("192.0.2.10")

    assert result.status is DnsStatus.FAIL


def test_fcrdns_is_unknown_when_dns_is_unavailable():
    resolver = _Resolver({
        ("10.2.0.192.in-addr.arpa", "PTR"): TimeoutError("resolver unavailable"),
    })

    result = FcrdnsAuditor(resolver).audit("192.0.2.10")

    assert result.status is DnsStatus.UNKNOWN


def test_fcrdns_is_unknown_when_one_forward_address_family_is_unavailable():
    resolver = _Resolver({
        ("1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa", "PTR"): ["mail.example.com."],
        ("mail.example.com", "A"): [],
        ("mail.example.com", "AAAA"): TimeoutError("resolver unavailable"),
    })

    result = FcrdnsAuditor(resolver).audit("2001:db8::1")

    assert result.status is DnsStatus.UNKNOWN


def test_readiness_requires_all_authentication_and_self_hosted_security_checks():
    report = evaluate_self_hosted_readiness(
        _ready_dns_audit(),
        fcrdns_status=DnsStatus.PASS,
        open_relay_status=DnsStatus.PASS,
    )

    assert report.ready is True
    assert report.blockers == ()


def test_readiness_never_treats_unknown_open_relay_result_as_safe():
    report = evaluate_self_hosted_readiness(
        _ready_dns_audit(),
        fcrdns_status=DnsStatus.PASS,
        open_relay_status=DnsStatus.UNKNOWN,
    )

    assert report.ready is False
    assert report.blockers == ("open_relay=UNKNOWN",)


def test_self_hosted_feature_flag_defaults_off_and_requires_exact_true(monkeypatch):
    monkeypatch.delenv("FTA_SELF_HOSTED_SMTP_ENABLED", raising=False)
    assert self_hosted_transport_enabled() is False

    monkeypatch.setenv("FTA_SELF_HOSTED_SMTP_ENABLED", "true")
    assert self_hosted_transport_enabled() is True

    monkeypatch.setenv("FTA_SELF_HOSTED_SMTP_ENABLED", "yes")
    assert self_hosted_transport_enabled() is False
