"""P9 self-hosted SMTP readiness report — audit only, never sends.

Examples:
    python tools/self_hosted_readiness.py --domain example.com \
        --sending-ip 203.0.113.10 --smtp-host smtp.example.com \
        --dkim-selector mail --open-relay-status PASS

``--open-relay-status PASS`` must come from an operator-reviewed external
security assessment.  This tool intentionally does not perform an active relay
probe, because readiness work must not create SMTP side effects.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from services.email_reputation.dns_audit import DnsAuditor
from services.email_reputation.models import DnsStatus
from services.email_reputation.self_hosted_readiness import (
    FcrdnsAuditor,
    evaluate_self_hosted_readiness,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P9 self-hosted SMTP readiness（只读）")
    parser.add_argument("--domain", required=True, help="发信域名")
    parser.add_argument("--sending-ip", required=True, help="候选 SMTP 公网 IP")
    parser.add_argument("--smtp-host", required=True, help="候选 SMTP 主机（仅 TLS 握手）")
    parser.add_argument("--smtp-port", type=int, default=465, help="implicit TLS 端口（默认 465）")
    parser.add_argument("--dkim-selector", help="已知 DKIM selector；不猜测")
    parser.add_argument(
        "--open-relay-status",
        choices=[status.value for status in DnsStatus],
        default=DnsStatus.UNKNOWN.value,
        help="外部安全审计结论；默认 UNKNOWN，绝不自动探测",
    )
    parser.add_argument(
        "--open-relay-report-id",
        help="外部 Open Relay 安全报告标识；PASS 时必填，保留在本次审计输出中",
    )
    args = parser.parse_args(argv)
    if args.open_relay_status == DnsStatus.PASS.value and not args.open_relay_report_id:
        parser.error("--open-relay-status PASS requires --open-relay-report-id")

    audit = DnsAuditor().audit(
        args.domain,
        dkim_selector=args.dkim_selector,
        sending_ip=args.sending_ip,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
    )
    fcrdns = FcrdnsAuditor().audit(args.sending_ip)
    report = evaluate_self_hosted_readiness(
        audit,
        fcrdns_status=fcrdns.status,
        open_relay_status=DnsStatus(args.open_relay_status),
    )

    print(f"domain: {audit.domain}")
    print(f"sending_ip: {fcrdns.sending_ip or 'UNKNOWN'}")
    print(f"ptr_hostname: {fcrdns.ptr_hostname or 'UNKNOWN'}")
    print(
        "dns: "
        f"MX={audit.mx_status.value} SPF={audit.spf_status.value} "
        f"DKIM={audit.dkim_status.value} DMARC={audit.dmarc_status.value} "
        f"A={audit.a_status.value} PTR={audit.ptr_status.value} "
        f"TLS={audit.tls_status.value} authentication={audit.authentication_status.value}"
    )
    print(f"fcrdns: {fcrdns.status.value}")
    print(f"open_relay: {args.open_relay_status}")
    if args.open_relay_report_id:
        print(f"open_relay_report_id: {args.open_relay_report_id}")
    print(f"ready: {'YES' if report.ready else 'NO'}")
    if report.blockers:
        print("blockers: " + ", ".join(report.blockers))
    print("transport remains OFF: P9 does not select or migrate SMTP traffic")
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
