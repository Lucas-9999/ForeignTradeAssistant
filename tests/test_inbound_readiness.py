"""P13 runtime prerequisites for normal outbound delivery."""

import pytest


@pytest.fixture(autouse=True)
def _synthetic_imap_settings(monkeypatch):
    """Exercise readiness branches with fake settings and mocked connections."""
    import config
    import emailer.inbound_readiness as readiness

    monkeypatch.setattr(config, "IMAP_HOST", "imap.portfolio.invalid")
    monkeypatch.setattr(config, "IMAP_USERNAME", "sender@portfolio.invalid")
    monkeypatch.setattr(config, "IMAP_PASSWORD", "synthetic-test-password")
    monkeypatch.setattr(readiness, "_cached_results", {})


def test_readiness_requires_the_imap_scheduler_before_connecting(monkeypatch):
    import emailer.inbound_readiness as readiness

    monkeypatch.setattr(readiness, "scheduler_is_running", lambda: False, raising=False)
    monkeypatch.setattr(
        readiness,
        "create_imap_connection",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("scheduler-off must not open IMAP")
        ),
    )

    ready, reason = readiness.normal_outbound_delivery_ready("default")

    assert ready is False
    assert "scheduler" in reason.lower()


def test_readiness_requires_an_accessible_inbox(monkeypatch):
    import emailer.inbound_readiness as readiness

    class _Connection:
        def __init__(self):
            self.selected = False

        def select(self, folder, readonly=False):
            self.selected = folder == "INBOX" and readonly
            return "NO", [b"denied"]

        def logout(self):
            raise AssertionError("failed INBOX selection must not logout as ready")

    connection = _Connection()
    monkeypatch.setattr(readiness, "scheduler_is_running", lambda: True)
    monkeypatch.setattr(readiness, "create_imap_connection", lambda *_args: connection)
    monkeypatch.setattr(readiness, "_cached_results", {})

    ready, reason = readiness.normal_outbound_delivery_ready("default")

    assert ready is False
    assert connection.selected is True
    assert "connection failed" in reason.lower()


def test_readiness_accepts_a_selectable_inbox_and_reuses_the_success_cache(monkeypatch):
    import emailer.inbound_readiness as readiness

    class _Connection:
        def __init__(self):
            self.select_calls = 0
            self.logged_out = False

        def select(self, folder, readonly=False):
            self.select_calls += 1
            assert folder == "INBOX"
            assert readonly is True
            return "OK", [b"1"]

        def logout(self):
            self.logged_out = True

    connection = _Connection()
    connect_calls = 0

    def _connect(*_args):
        nonlocal connect_calls
        connect_calls += 1
        return connection

    monkeypatch.setattr(readiness, "scheduler_is_running", lambda: True)
    monkeypatch.setattr(readiness, "create_imap_connection", _connect)
    monkeypatch.setattr(readiness, "_cached_results", {})

    assert readiness.normal_outbound_delivery_ready("default") == (
        True,
        "default IMAP monitor ready",
    )
    assert connection.select_calls == 1
    assert connection.logged_out is True
    assert readiness.normal_outbound_delivery_ready("default") == (
        True,
        "default IMAP monitor ready",
    )
    assert connect_calls == 1
