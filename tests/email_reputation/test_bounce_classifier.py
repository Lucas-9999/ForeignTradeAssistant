from pathlib import Path

import pytest

from services.email_reputation.bounce_classifier import (
    BounceCategory,
    classify_bounce,
)
from services.email_reputation.smtp_error_parser import parse_smtp_error


FIXTURES = Path(__file__).with_name("fixtures")


def test_unknown_recipient_is_a_hard_bounce():
    diagnostic = parse_smtp_error("550 5.1.1 User unknown")

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.HARD
    assert result.is_retryable is False
    assert result.should_suppress_recipient is True


def test_mailbox_full_is_soft_even_when_smtp_code_is_552():
    diagnostic = parse_smtp_error("552 5.2.2 Mailbox full")

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.SOFT
    assert result.is_retryable is True
    assert result.should_suppress_recipient is False


def test_ip_reputation_rejection_is_not_a_hard_bounce():
    diagnostic = parse_smtp_error(
        "554 5.7.1 Message rejected due to sending IP reputation"
    )

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.REPUTATION
    assert result.should_suppress_recipient is False


def test_provider_rate_limit_is_separate_from_generic_soft_bounce():
    diagnostic = parse_smtp_error(
        "421 4.7.0 Temporarily deferred: too many messages from this sender"
    )

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.RATE_LIMIT
    assert result.is_retryable is True
    assert result.should_suppress_recipient is False


def test_authentication_failure_is_a_sender_problem():
    diagnostic = parse_smtp_error(
        "535 5.7.8 Authentication credentials invalid"
    )

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.AUTHENTICATION
    assert result.should_suppress_recipient is False


def test_policy_rejection_does_not_invalidate_the_recipient():
    diagnostic = parse_smtp_error("550 5.7.1 Relay denied by recipient policy")

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.POLICY
    assert result.should_suppress_recipient is False


def test_message_content_rejection_is_classified_separately():
    diagnostic = parse_smtp_error(
        "554 5.7.1 Message content rejected: prohibited attachment type"
    )

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.CONTENT
    assert result.should_suppress_recipient is False


@pytest.mark.parametrize(
    ("fixture_name", "provider", "expected"),
    [
        ("netease_invalid_recipient.txt", "netease", BounceCategory.HARD),
        ("zoho_mailbox_full.txt", "zoho", BounceCategory.SOFT),
        ("google_ip_reputation.txt", "google", BounceCategory.REPUTATION),
        ("microsoft_rate_limit.txt", "microsoft", BounceCategory.RATE_LIMIT),
    ],
)
def test_provider_and_dsn_fixtures_have_stable_categories(
    fixture_name, provider, expected
):
    diagnostic = parse_smtp_error(
        (FIXTURES / fixture_name).read_text(encoding="utf-8"),
        provider=provider,
    )

    result = classify_bounce(diagnostic)

    assert result.category is expected
    assert result.should_suppress_recipient is (expected is BounceCategory.HARD)


def test_generic_5xx_is_not_automatically_a_hard_bounce():
    diagnostic = parse_smtp_error("554 5.0.0 Transaction failed")

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.UNKNOWN
    assert result.should_suppress_recipient is False


def test_bad_destination_address_syntax_is_a_hard_bounce():
    diagnostic = parse_smtp_error("553 5.1.3 Bad destination mailbox syntax")

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.HARD
    assert result.should_suppress_recipient is True


def test_user_not_local_without_forwarding_is_a_hard_bounce():
    diagnostic = parse_smtp_error("551 User not local; no forwarding address")

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.HARD
    assert result.should_suppress_recipient is True


def test_specific_invalid_recipient_code_beats_vague_policy_text():
    diagnostic = parse_smtp_error(
        "550 5.1.1 Recipient rejected: policy rejection at destination"
    )

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.HARD
    assert result.should_suppress_recipient is True


def test_sender_address_rejection_does_not_suppress_the_recipient():
    diagnostic = parse_smtp_error("553 Sender address rejected: not owned by user")

    result = classify_bounce(diagnostic)

    assert result.category is not BounceCategory.HARD
    assert result.should_suppress_recipient is False


def test_nonexistent_destination_domain_is_a_hard_bounce():
    diagnostic = parse_smtp_error("550 Recipient domain does not exist")

    result = classify_bounce(diagnostic)

    assert result.category is BounceCategory.HARD
    assert result.should_suppress_recipient is True


def test_nonexistent_sender_domain_does_not_suppress_the_recipient():
    diagnostic = parse_smtp_error("550 Sender domain does not exist")

    result = classify_bounce(diagnostic)

    assert result.category is not BounceCategory.HARD
    assert result.should_suppress_recipient is False


def test_generic_recipient_rejection_is_not_enough_for_suppression():
    diagnostic = parse_smtp_error("550 Recipient rejected")

    result = classify_bounce(diagnostic)

    assert result.category is not BounceCategory.HARD
    assert result.should_suppress_recipient is False


def test_classification_signals_survive_bounded_public_message():
    diagnostic = parse_smtp_error(("x" * 2100) + " rate limit exceeded")

    result = classify_bounce(diagnostic)

    assert len(diagnostic.message) == 2000
    assert result.category is BounceCategory.RATE_LIMIT
