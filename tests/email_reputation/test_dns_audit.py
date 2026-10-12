"""Phase 5 DNS audit tests — all resolver interaction is injected/mocked."""

from __future__ import annotations

import sqlite3
import ssl

from crm.db import build_database
from services.email_reputation.dns_audit import DnsAuditor, default_resolver
from services.email_reputation.health_scoring import (
    persist_domain_audit,
    score_domain_audit,
)
from services.email_reputation.models import (
    DnsStatus,
    HealthStatus,
    ReputationEntityType,
)
from services.email_reputation.repository import EmailReputationRepository


class _Resolver:
    def __init__(self, answers):
        self.answers = answers

    def resolve(self, name, record_type):
        answer = self.answers.get((name, record_type), [])
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_default_resolver_has_bounded_network_timeout():
    resolver = default_resolver()

    assert resolver.timeout <= 1.0
    assert resolver.lifetime <= 2.0


def test_audit_reports_authenticated_domain_without_network():
    resolver = _Resolver({
        ("example.com", "MX"): ["10 mx.example.com."],
        ("example.com", "A"): ["192.0.2.10"],
        ("example.com", "AAAA"): [],
        ("example.com", "TXT"): ["v=spf1 include:mail.example.com -all"],
        ("_dmarc.example.com", "TXT"): ["v=DMARC1; p=quarantine"],
        ("mail._domainkey.example.com", "TXT"): ["v=DKIM1; k=rsa; p=abc"],
    })

    audit = DnsAuditor(resolver).audit("Example.COM.", dkim_selector="mail")

    assert audit.domain == "example.com"
    assert audit.mx_status is DnsStatus.PASS
    assert audit.a_status is DnsStatus.PASS
    assert audit.spf_status is DnsStatus.PASS
    assert audit.dmarc_status is DnsStatus.PASS
    assert audit.dkim_status is DnsStatus.PASS
    assert audit.authentication_status is DnsStatus.PASS


def test_optional_ptr_and_tls_checks_use_explicit_configuration_only():
    resolver = _Resolver({
        ("example.com", "MX"): ["10 mx.example.com."],
        ("example.com", "A"): ["192.0.2.10"],
        ("example.com", "AAAA"): [],
        ("example.com", "TXT"): ["v=spf1 -all"],
        ("_dmarc.example.com", "TXT"): ["v=DMARC1; p=none"],
        ("10.2.0.192.in-addr.arpa", "PTR"): ["mail.example.com."],
    })
    tls_calls = []

    def tls_checker(host, port):
        tls_calls.append((host, port))
        return True

    audit = DnsAuditor(resolver, tls_checker=tls_checker).audit(
        "example.com",
        sending_ip="192.0.2.10",
        smtp_host="smtp.example.com",
        smtp_port=465,
    )

    assert audit.ptr_status is DnsStatus.PASS
    assert audit.tls_status is DnsStatus.PASS
    assert tls_calls == [("smtp.example.com", 465)]


def test_tls_network_error_is_unknown_not_a_tls_failure():
    resolver = _Resolver({})

    def unavailable_tls(host, port):
        raise TimeoutError("network unavailable")

    audit = DnsAuditor(resolver, tls_checker=unavailable_tls).audit(
        "example.com", smtp_host="smtp.example.com", smtp_port=465
    )

    assert audit.tls_status is DnsStatus.UNKNOWN


def test_tls_certificate_failure_is_a_deterministic_fail():
    def invalid_certificate(host, port):
        raise ssl.SSLCertVerificationError(1, "certificate verify failed")

    audit = DnsAuditor(_Resolver({}), tls_checker=invalid_certificate).audit(
        "example.com", smtp_host="smtp.example.com", smtp_port=465
    )

    assert audit.tls_status is DnsStatus.FAIL


def test_timeout_is_unknown_not_failed():
    resolver = _Resolver({
        ("example.com", "MX"): TimeoutError("resolver unavailable"),
        ("example.com", "A"): TimeoutError("resolver unavailable"),
        ("example.com", "AAAA"): TimeoutError("resolver unavailable"),
        ("example.com", "TXT"): TimeoutError("resolver unavailable"),
        ("_dmarc.example.com", "TXT"): TimeoutError("resolver unavailable"),
    })

    audit = DnsAuditor(resolver).audit("example.com")

    assert {audit.mx_status, audit.a_status, audit.spf_status, audit.dmarc_status} == {
        DnsStatus.UNKNOWN
    }
    assert audit.dkim_status is DnsStatus.UNKNOWN
    assert audit.authentication_status is DnsStatus.UNKNOWN
    score = score_domain_audit(audit)
    assert score.status is HealthStatus.WARNING
    assert score.health_score < 100


def test_no_nameservers_is_unknown_not_missing_record():
    class NoNameservers(Exception):
        pass

    resolver = _Resolver({
        ("example.com", "MX"): NoNameservers("all resolvers unavailable"),
        ("example.com", "A"): NoNameservers("all resolvers unavailable"),
        ("example.com", "AAAA"): NoNameservers("all resolvers unavailable"),
        ("example.com", "TXT"): NoNameservers("all resolvers unavailable"),
        ("_dmarc.example.com", "TXT"): NoNameservers("all resolvers unavailable"),
    })

    audit = DnsAuditor(resolver).audit("example.com")

    assert audit.mx_status is DnsStatus.UNKNOWN
    assert audit.a_status is DnsStatus.UNKNOWN


def test_missing_records_are_distinguished_from_resolver_outage():
    audit = DnsAuditor(_Resolver({})).audit("example.com")

    assert audit.mx_status is DnsStatus.FAIL
    assert audit.a_status is DnsStatus.FAIL
    assert audit.spf_status is DnsStatus.WARNING
    assert audit.dmarc_status is DnsStatus.WARNING
    assert audit.dkim_status is DnsStatus.UNKNOWN


def test_authentication_warning_is_not_hidden_by_unknown_dkim_selector():
    resolver = _Resolver({
        ("example.com", "MX"): ["10 mx.example.com."],
        ("example.com", "A"): ["192.0.2.10"],
        ("example.com", "AAAA"): [],
        ("example.com", "TXT"): [],
        ("_dmarc.example.com", "TXT"): ["v=DMARC1; p=none"],
    })

    audit = DnsAuditor(resolver).audit("example.com")

    assert audit.spf_status is DnsStatus.WARNING
    assert audit.dkim_status is DnsStatus.UNKNOWN
    assert audit.authentication_status is DnsStatus.WARNING


def test_shadow_score_never_creates_a_guarding_status():
    audit = DnsAuditor(_Resolver({})).audit("example.com")

    score = score_domain_audit(audit)

    assert 0 <= score.health_score <= 100
    assert score.status is HealthStatus.WARNING
    assert score.status not in {
        HealthStatus.RESTRICTED,
        HealthStatus.PAUSED,
        HealthStatus.DISABLED,
    }


def test_explicit_persist_writes_snapshot_and_only_events_on_change(tmp_path):
    path = tmp_path / "dns_audit.db"
    build_database(path).close()
    conn = sqlite3.connect(path)
    try:
        repository = EmailReputationRepository(conn)
        audit = DnsAuditor(_Resolver({})).audit("example.com")

        snapshot, event = persist_domain_audit(
            repository, audit, checked_at="2026-08-22 10:00:00"
        )

        assert snapshot.status is HealthStatus.WARNING
        assert event is not None
        assert event.entity_type is ReputationEntityType.DOMAIN
        assert len(repository.list_reputation_events(
            entity_type=ReputationEntityType.DOMAIN, entity_id="example.com"
        )) == 1

        _, unchanged_event = persist_domain_audit(
            repository, audit, checked_at="2026-08-22 11:00:00"
        )
        assert unchanged_event is None
    finally:
        conn.close()


def test_persist_keeps_sending_ip_and_audits_ptr_tls_changes(tmp_path):
    path = tmp_path / "dns_ptr_tls.db"
    build_database(path).close()
    conn = sqlite3.connect(path)
    resolver = _Resolver({
        ("example.com", "MX"): ["10 mx.example.com."],
        ("example.com", "A"): ["192.0.2.10"],
        ("example.com", "AAAA"): [],
        ("example.com", "TXT"): ["v=spf1 -all"],
        ("_dmarc.example.com", "TXT"): ["v=DMARC1; p=none"],
        ("10.2.0.192.in-addr.arpa", "PTR"): ["mail.example.com."],
    })
    try:
        repository = EmailReputationRepository(conn)
        first = DnsAuditor(resolver, tls_checker=lambda host, port: True).audit(
            "example.com",
            sending_ip="192.0.2.10",
            smtp_host="smtp.example.com",
        )
        snapshot, event = persist_domain_audit(
            repository, first, checked_at="2026-08-22 10:00:00"
        )
        assert snapshot.sending_ip == "192.0.2.10"
        assert event is not None
        assert event.metadata["ptr_status"] == "PASS"
        assert event.metadata["tls_status"] == "PASS"

        tls_failed = DnsAuditor(resolver, tls_checker=lambda host, port: False).audit(
            "example.com",
            sending_ip="192.0.2.10",
            smtp_host="smtp.example.com",
        )
        _, changed_event = persist_domain_audit(
            repository, tls_failed, checked_at="2026-08-22 11:00:00"
        )
        assert changed_event is not None
        assert changed_event.metadata["tls_status"] == "FAIL"
    finally:
        conn.close()
