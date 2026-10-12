import pytest

from services.email_reputation.smtp_error_parser import (
    build_smtp_diagnostic_event,
    parse_smtp_error,
)


def test_parser_extracts_smtp_and_enhanced_status_codes():
    parsed = parse_smtp_error(
        "550 5.1.1 The email account that you tried to reach does not exist",
        provider="google",
        recipient_domain="Example.COM",
    )

    assert parsed.smtp_code == 550
    assert parsed.enhanced_status_code == "5.1.1"
    assert parsed.provider == "google"
    assert parsed.recipient_domain == "example.com"


def test_parser_redacts_credentials_and_recipient_addresses():
    parsed = parse_smtp_error(
        "550 5.1.1 buyer@example.com password=hunter2 "
        "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=="
    )

    assert "buyer@example.com" not in parsed.message
    assert "hunter2" not in parsed.message
    assert "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" not in parsed.message
    assert "[EMAIL_REDACTED]" in parsed.message
    assert "[REDACTED]" in parsed.message


def test_parser_rejects_invalid_recipient_domain_context():
    with pytest.raises(ValueError, match="recipient_domain"):
        parse_smtp_error("550 rejected", recipient_domain="https://example.com")


def test_parser_does_not_mistake_an_ip_address_for_enhanced_status():
    parsed = parse_smtp_error("Message from [5.1.1.1] was rejected")

    assert parsed.smtp_code is None
    assert parsed.enhanced_status_code is None


def test_parser_normalizes_caller_supplied_observation_time_to_utc():
    parsed = parse_smtp_error(
        "451 temporary failure",
        observed_at="2026-08-18T10:30:00+08:00",
    )

    assert parsed.observed_at == "2026-08-18T02:30:00Z"


def test_parser_bounds_large_provider_diagnostics():
    parsed = parse_smtp_error("451 temporary failure " + ("x" * 5000))

    assert len(parsed.message) == 2000


def test_parser_extracts_codes_that_appear_after_the_message_boundary():
    parsed = parse_smtp_error(("x" * 2100) + " Diagnostic-Code: 550 5.1.1")

    assert parsed.smtp_code == 550
    assert parsed.enhanced_status_code == "5.1.1"
    assert len(parsed.message) == 2000


@pytest.mark.parametrize(
    "address",
    [
        "用户@例子.公司",
        '"quoted local"@example.com',
        "buyer@[192.0.2.1]",
    ],
)
def test_parser_redacts_international_quoted_and_literal_addresses(address):
    parsed = parse_smtp_error(f"550 rejected recipient {address}")

    assert address not in parsed.message
    assert "[EMAIL_REDACTED]" in parsed.message


def test_complete_diagnostic_event_requires_and_normalizes_context():
    event = build_smtp_diagnostic_event(
        "550 5.1.1 buyer@example.com does not exist",
        provider="NetEase",
        recipient_domain="Example.COM",
        observed_at="2026-08-18T10:30:00+08:00",
    )

    assert event.smtp_code == 550
    assert event.enhanced_status_code == "5.1.1"
    assert event.provider == "netease"
    assert event.recipient_domain == "example.com"
    assert event.observed_at == "2026-08-18T02:30:00Z"
    assert "buyer@example.com" not in event.message
