"""Facebook session health-check resilience tests (data-collection fix).

Regression: 2026-08-07..08-20 the health check hard-failed on a single 15s
`page.goto(mbasic)` timeout (no retry, no www fallback on timeout), so every
3-day Facebook run returned 0 leads. After the fix the check goes through
`_load_page` (retry + auth detection) and routes network-level mbasic failure
to the www fallback instead of failing.

These tests drive the check with a fake page + a stubbed `_load_page` — no real
browser, no network.
"""

import asyncio

from collector.retry import AuthErrorDetected, MaxRetriesExceeded
from collector.sources.facebook import FB_BASE, FB_WWW, FacebookCollector


class _FakePage:
    def __init__(self):
        self.url = FB_BASE


class _PageCM:
    def __init__(self, page):
        self.page = page

    async def __aenter__(self):
        return self.page

    async def __aexit__(self, *exc):
        return False


class _FakeContext:
    """new_page() returns an awaitable that yields an async CM for a fake page."""

    def __init__(self, page):
        self.page = page

    def new_page(self):
        async def _mk():
            return _PageCM(self.page)

        return _mk()


class _FakeLimiter:
    def __init__(self):
        self.failures = 0
        self.successes = 0

    def report_failure(self):
        self.failures += 1

    def report_success(self):
        self.successes += 1

    def report_rate_limited(self):
        pass


def _collector(page, headless=False):
    inst = object.__new__(FacebookCollector)
    inst._context = _FakeContext(page)
    inst._use_www = False
    inst._session_healthy = False
    inst.headless = headless
    inst._rate_limiter = _FakeLimiter()
    return inst


def _results_content():
    """A parseable search-results page with one group link (not login/block)."""
    return '<a href="/groups/123">Fastener Traders</a>'


def test_mbasic_timeout_falls_back_to_www():
    """A network-level mbasic timeout must fall back to www, not hard-fail."""
    page = _FakePage()
    inst = _collector(page, headless=False)
    calls = []

    async def fake_load(page_, url, timeout=30000, label=""):
        calls.append((url, label))
        if url == FB_BASE:
            raise asyncio.TimeoutError("Page.goto: Timeout 15000ms exceeded")
        page_.url = url
        return _results_content()

    # Instance attribute shadows the class method; use a plain async function.
    async def shadow_load(*args, **kwargs):
        return await fake_load(*args, **kwargs)

    inst._load_page = shadow_load

    result = asyncio.run(inst._session_health_check())
    assert result is True
    assert inst._session_healthy is True
    assert inst._use_www is True  # www enabled so collection can proceed
    assert any(FB_WWW in u for u, _ in calls), "www fallback was not attempted"


def test_mbasic_retries_exhausted_falls_back_to_www():
    page = _FakePage()
    inst = _collector(page, headless=False)
    calls = []

    async def fake_load(page_, url, timeout=30000, label=""):
        calls.append(url)
        if url == FB_BASE:
            raise MaxRetriesExceeded("fb_health-mbasic", 3)
        page_.url = url
        return _results_content()

    async def shadow_load(*args, **kwargs):
        return await fake_load(*args, **kwargs)

    inst._load_page = shadow_load

    result = asyncio.run(inst._session_health_check())
    assert result is True
    assert inst._use_www is True
    assert any(c.startswith(FB_WWW) for c in calls)


def test_auth_block_health_check_fails_fast():
    """AuthErrorDetected (login/checkpoint/block) is a real session failure —
    no www fallback, fail fast."""
    page = _FakePage()
    inst = _collector(page)

    async def fake_load(page_, url, timeout=30000, label=""):
        raise AuthErrorDetected("fb", "login page detected")

    async def shadow_load(*args, **kwargs):
        return await fake_load(*args, **kwargs)

    inst._load_page = shadow_load

    result = asyncio.run(inst._session_health_check())
    assert result is False
    assert inst._session_healthy is False
    assert inst._use_www is False


def test_shadow_block_zero_results_hard_fails():
    """0 parseable result links is a shadow-block — still a hard failure."""
    page = _FakePage()
    inst = _collector(page)

    async def fake_load(page_, url, timeout=30000, label=""):
        page_.url = url
        return "<html><body>no results</body></html>"

    async def shadow_load(*args, **kwargs):
        return await fake_load(*args, **kwargs)

    inst._load_page = shadow_load

    result = asyncio.run(inst._session_health_check())
    assert result is False
    assert inst._session_healthy is False


def test_healthy_mbasic_passes_without_www():
    page = _FakePage()
    inst = _collector(page)

    async def fake_load(page_, url, timeout=30000, label=""):
        page_.url = url
        return _results_content()

    async def shadow_load(*args, **kwargs):
        return await fake_load(*args, **kwargs)

    inst._load_page = shadow_load

    result = asyncio.run(inst._session_health_check())
    assert result is True
    assert inst._session_healthy is True
    assert inst._use_www is False
