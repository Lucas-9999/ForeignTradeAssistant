"""P9 CLI report tests — all DNS/TLS inputs are injected; no network/SMTP."""

from __future__ import annotations

import pytest

from services.email_reputation.dns_audit import DomainDnsAudit
from services.email_reputation.models import DnsStatus


def _audit() -> DomainDnsAudit:
    return DomainDnsAudit(
        domain="example.com", sending_ip="192.0.2.10",
        mx_status=DnsStatus.PASS, spf_status=DnsStatus.PASS,
        dkim_status=DnsStatus.PASS, dmarc_status=DnsStatus.PASS,
        a_status=DnsStatus.PASS, ptr_status=DnsStatus.PASS,
        tls_status=DnsStatus.PASS, authentication_status=DnsStatus.PASS,
    )


def test_readiness_cli_reports_evidence_without_selecting_a_transport(monkeypatch, capsys):
    from tools import self_hosted_readiness as tool

    class _DnsAuditor:
        def audit(self, *args, **kwargs):
            return _audit()

    class _FcrdnsAuditor:
        def audit(self, sending_ip):
            return type("Result", (), {
                "sending_ip": sending_ip,
                "ptr_hostname": "mail.example.com",
                "status": DnsStatus.PASS,
            })()

    monkeypatch.setattr(tool, "DnsAuditor", _DnsAuditor)
    monkeypatch.setattr(tool, "FcrdnsAuditor", _FcrdnsAuditor)

    rc = tool.main([
        "--domain", "example.com", "--sending-ip", "192.0.2.10",
        "--smtp-host", "smtp.example.com", "--open-relay-status", "PASS",
        "--open-relay-report-id", "security-review-2026-08-22",
    ])

    assert rc == 0
    output = capsys.readouterr().out
    assert "ready: YES" in output
    assert "A=PASS" in output
    assert "authentication=PASS" in output
    assert "open_relay_report_id: security-review-2026-08-22" in output
    assert "transport remains OFF" in output


def test_readiness_cli_defaults_open_relay_to_unknown_and_refuses_readiness(monkeypatch, capsys):
    from tools import self_hosted_readiness as tool

    class _DnsAuditor:
        def audit(self, *args, **kwargs):
            return _audit()

    class _FcrdnsAuditor:
        def audit(self, sending_ip):
            return type("Result", (), {
                "sending_ip": sending_ip,
                "ptr_hostname": "mail.example.com",
                "status": DnsStatus.PASS,
            })()

    monkeypatch.setattr(tool, "DnsAuditor", _DnsAuditor)
    monkeypatch.setattr(tool, "FcrdnsAuditor", _FcrdnsAuditor)

    rc = tool.main([
        "--domain", "example.com", "--sending-ip", "192.0.2.10",
        "--smtp-host", "smtp.example.com",
    ])

    assert rc == 2
    output = capsys.readouterr().out
    assert "open_relay=UNKNOWN" in output


def test_readiness_cli_refuses_unattributed_open_relay_pass(monkeypatch):
    from tools import self_hosted_readiness as tool

    class _DnsAuditor:
        def audit(self, *args, **kwargs):
            return _audit()

    class _FcrdnsAuditor:
        def audit(self, sending_ip):
            return type("Result", (), {
                "sending_ip": sending_ip,
                "ptr_hostname": "mail.example.com",
                "status": DnsStatus.PASS,
            })()

    monkeypatch.setattr(tool, "DnsAuditor", _DnsAuditor)
    monkeypatch.setattr(tool, "FcrdnsAuditor", _FcrdnsAuditor)

    with pytest.raises(SystemExit) as exc:
        tool.main([
            "--domain", "example.com", "--sending-ip", "192.0.2.10",
            "--smtp-host", "smtp.example.com", "--open-relay-status", "PASS",
        ])

    assert exc.value.code == 2
