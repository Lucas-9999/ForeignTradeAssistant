"""
Base Collector — abstract foundation for all data source collectors.

Pattern: mirrors PriceGod's approach of keeping base classes lightweight.
Each collector is an independent module that only needs to implement
`async def collect() -> list[dict]`.

For API-first collectors with advanced risk control, use the dedicated modules:
  - collector.rate_limiter.AdaptiveRateLimiter (token bucket + jitter + circuit breaker)
  - collector.retry.async_retry (exponential backoff + auth error detection)

This BaseCollector provides a simple rate-limited HTTP helper for lightweight
collectors (e.g. static website scraping).
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Optional

import httpx

import config

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Abstract base for all lead source collectors.

    Subclasses must:
      - Set `source_name` (matches Lead.source values)
      - Implement `async def collect() -> list[dict]`

    The returned dicts should have keys matching Lead fields:
      company_name, contact_name, email, phone, whatsapp, website,
      industry, country, source_url, source_detail
    """

    source_name: str = ""  # e.g. "xunkexing", "facebook", "industry_site"

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        delay: float = config.SCRAPE_REQUEST_DELAY,
        user_agent: str = config.SCRAPE_USER_AGENT,
        timeout: float = 30.0,
        jitter_factor: float = 0.3,
    ):
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Sec-Ch-Ua": (
                    '"Chromium";v="120", "Google Chrome";v="120", '
                    '"Not?A_Brand";v="99"'
                ),
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "Dnt": "1",
            },
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
        )
        self.delay = delay
        self.jitter_factor = jitter_factor
        self.user_agent = user_agent
        self.timeout = timeout
        self._last_request_time = 0.0

    # ── HTTP helpers ──────────────────────────────────────────────────────

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        """Rate-limited GET request with jitter. Respects SCRAPE_REQUEST_DELAY."""
        await self._wait_delay()
        logger.debug(f"GET {url}")
        resp = await self._client.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    async def _post(self, url: str, **kwargs) -> httpx.Response:
        """Rate-limited POST request with jitter."""
        await self._wait_delay()
        logger.debug(f"POST {url}")
        resp = await self._client.post(url, **kwargs)
        resp.raise_for_status()
        return resp

    async def _wait_delay(self) -> None:
        """Enforce minimum delay between requests, with random jitter.

        Jitter prevents robotic traffic patterns that trigger anti-bot detection.
        The actual delay is: base_delay * (1 +/- jitter_factor)
        """
        elapsed = time.monotonic() - self._last_request_time

        # Apply jitter to the base delay
        if self.jitter_factor > 0:
            jitter_range = self.delay * self.jitter_factor
            jittered_delay = self.delay + random.uniform(-jitter_range, jitter_range)
            effective_delay = max(0.1, jittered_delay)
        else:
            effective_delay = self.delay

        if elapsed < effective_delay:
            await self._async_sleep(effective_delay - elapsed)
        self._last_request_time = time.monotonic()

    @staticmethod
    async def _async_sleep(seconds: float) -> None:
        """Async sleep — overrideable for testing."""
        import asyncio
        await asyncio.sleep(seconds)

    # ── Abstract interface ────────────────────────────────────────────────

    @abstractmethod
    async def collect(self) -> list[dict]:
        """Run collection and return a list of raw lead dicts.

        Each dict should contain keys matching Lead fields:
        - company_name (str)
        - contact_name (str | None)
        - email (str | None)
        - phone (str | None)
        - whatsapp (str | None)
        - website (str | None)
        - industry (str | None)
        - country (str | None)
        - source_url (str | None)
        - source_detail (dict | None) — extra metadata stored as JSON
        """
        ...

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Release resources (HTTP client, browser, etc.)."""
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
