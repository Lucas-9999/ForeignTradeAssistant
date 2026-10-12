"""
Adaptive Rate Limiter — token-bucket + jitter + adaptive slowdown.

Prevents overwhelming target servers and reduces detection risk by:
1. Token bucket: enforces a max requests-per-minute ceiling
2. Jitter: randomizes delays so traffic doesn't look robotic
3. Adaptive slowdown: increases delay after errors, decreases after successes
4. Retry-After: respects server-sent rate-limit headers (429 responses)

Usage:
    limiter = AdaptiveRateLimiter(requests_per_minute=30)
    async with limiter:
        response = await client.get(...)
        limiter.report_success()
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RateLimiterConfig:
    """Configuration for the adaptive rate limiter."""

    # Token bucket: max requests per minute (0 = unlimited)
    requests_per_minute: int = 30

    # Base delay between requests in seconds
    min_delay: float = 1.0

    # Maximum delay (adaptive backoff ceiling)
    max_delay: float = 30.0

    # Jitter: randomize delay by +/- this factor (0.3 = +/- 30%)
    jitter_factor: float = 0.3

    # Adaptive: multiply delay by this factor on each error
    backoff_multiplier: float = 1.5

    # Adaptive: divide delay by this factor on each success (gradual recovery)
    recovery_divider: float = 1.1

    # Consecutive successes before reducing delay
    successes_for_recovery: int = 5

    # Maximum consecutive errors before circuit opens
    max_consecutive_errors: int = 8

    # Cooldown after circuit opens, in seconds
    circuit_cooldown_seconds: float = 300.0


class CircuitBreakerOpen(Exception):
    """Raised when the circuit breaker is open — stop requesting this target."""

    def __init__(self, source: str, cooldown_remaining: float):
        self.source = source
        self.cooldown_remaining = cooldown_remaining
        super().__init__(
            f"Circuit breaker OPEN for '{source}': "
            f"{cooldown_remaining:.0f}s cooldown remaining"
        )


class AdaptiveRateLimiter:
    """Token-bucket rate limiter with jitter and adaptive slowdown.

    Thread-safe for single-consumer async usage. Not safe for multi-threaded use.

    Key behaviors:
    - Enforces requests_per_minute via token bucket
    - Adds random jitter to every delay
    - Increases delay after errors (adaptive backoff)
    - Gradually recovers to baseline after sustained success
    - Opens circuit breaker after max_consecutive_errors
    - Respects server-sent Retry-After on 429 responses
    """

    def __init__(
        self,
        config: Optional[RateLimiterConfig] = None,
        source_name: str = "unknown",
        **kwargs,
    ):
        cfg = config or RateLimiterConfig()
        # Allow overriding config fields via kwargs
        for key, value in kwargs.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

        self._cfg = cfg
        self.source_name = source_name

        # Token bucket state
        self._tokens: float = float(cfg.requests_per_minute) if cfg.requests_per_minute > 0 else float("inf")
        self._max_tokens: float = float(cfg.requests_per_minute) if cfg.requests_per_minute > 0 else float("inf")
        self._last_refill: float = time.monotonic()
        self._token_refill_rate: float = cfg.requests_per_minute / 60.0 if cfg.requests_per_minute > 0 else 0.0

        # Adaptive state
        self._current_delay: float = cfg.min_delay
        self._consecutive_errors: int = 0
        self._consecutive_successes: int = 0
        self._circuit_open_until: float = 0.0
        self._total_requests: int = 0
        self._total_errors: int = 0
        self._total_rate_limits: int = 0

    # ── Core API ──────────────────────────────────────────────────────────

    async def acquire(self) -> float:
        """Wait until a request is permitted. Returns the actual wait time in seconds.

        Raises CircuitBreakerOpen if the circuit is currently open.
        """
        # Check circuit breaker
        if self._circuit_open_until > 0:
            remaining = self._circuit_open_until - time.monotonic()
            if remaining > 0:
                raise CircuitBreakerOpen(self.source_name, remaining)
            # Cooldown expired, reset circuit
            logger.info(
                f"[{self.source_name}] Circuit breaker cooldown expired, resetting"
            )
            self._circuit_open_until = 0.0
            self._consecutive_errors = 0
            self._current_delay = self._cfg.min_delay

        wait_time = 0.0

        # Token bucket: wait if no tokens available
        if self._token_refill_rate > 0:
            self._refill_tokens()
            if self._tokens < 1.0:
                token_wait = (1.0 - self._tokens) / self._token_refill_rate
                await asyncio.sleep(token_wait)
                wait_time += token_wait
                self._refill_tokens()

        # Consume a token
        if self._max_tokens != float("inf"):
            self._tokens -= 1.0

        # Apply adaptive delay with jitter
        delay = self._compute_delay()
        if delay > 0:
            await asyncio.sleep(delay)
            wait_time += delay

        self._total_requests += 1
        return wait_time

    def _refill_tokens(self):
        """Refill token bucket based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self._token_refill_rate
        self._tokens = min(self._max_tokens, self._tokens + new_tokens)
        self._last_refill = now

    def _compute_delay(self) -> float:
        """Compute the current delay with jitter."""
        base = self._current_delay
        if self._cfg.jitter_factor > 0:
            jitter_range = base * self._cfg.jitter_factor
            delay = base + random.uniform(-jitter_range, jitter_range)
        else:
            delay = base
        return max(0, delay)

    # ── Feedback: report outcomes to adjust behavior ──────────────────────

    def report_success(self, latency: Optional[float] = None):
        """Report a successful request. Gradually reduces adaptive delay."""
        self._consecutive_errors = 0
        self._consecutive_successes += 1

        # Gradual recovery: after N successes, reduce delay
        if self._consecutive_successes >= self._cfg.successes_for_recovery:
            new_delay = self._current_delay / self._cfg.recovery_divider
            if new_delay < self._cfg.min_delay:
                new_delay = self._cfg.min_delay
            if new_delay != self._current_delay:
                logger.debug(
                    f"[{self.source_name}] Recovering: delay {self._current_delay:.1f}s "
                    f"→ {new_delay:.1f}s (after {self._consecutive_successes} successes)"
                )
                self._current_delay = new_delay
            self._consecutive_successes = 0

    def report_failure(self, status_code: Optional[int] = None):
        """Report a failed request. Increases adaptive delay."""
        self._consecutive_errors += 1
        self._consecutive_successes = 0
        self._total_errors += 1

        # Increase delay
        new_delay = self._current_delay * self._cfg.backoff_multiplier
        if new_delay > self._cfg.max_delay:
            new_delay = self._cfg.max_delay
        if new_delay != self._current_delay:
            logger.warning(
                f"[{self.source_name}] Backing off: delay {self._current_delay:.1f}s "
                f"→ {new_delay:.1f}s (error #{self._consecutive_errors}, "
                f"status={status_code})"
            )
            self._current_delay = new_delay

        # Check circuit breaker threshold
        if self._consecutive_errors >= self._cfg.max_consecutive_errors:
            self._open_circuit()

    def report_rate_limited(self, retry_after: Optional[float] = None):
        """Report a 429 (rate limit) response.

        This is more severe than a generic failure — we respect the server's
        Retry-After header and increase our internal delay significantly.
        """
        self._total_rate_limits += 1
        self._consecutive_errors += 1
        self._consecutive_successes = 0

        # Respect server's Retry-After
        if retry_after and retry_after > 0:
            effective_delay = min(retry_after, self._cfg.max_delay)
        else:
            # Without Retry-After, double the current delay
            effective_delay = min(
                self._current_delay * self._cfg.backoff_multiplier * 2,
                self._cfg.max_delay,
            )

        logger.warning(
            f"[{self.source_name}] Rate limited (429). "
            f"Setting delay to {effective_delay:.1f}s "
            f"(retry_after={retry_after})"
        )
        self._current_delay = effective_delay

        # Check circuit breaker
        if self._consecutive_errors >= self._cfg.max_consecutive_errors:
            self._open_circuit()

    def _open_circuit(self):
        """Open the circuit breaker — stop all requests to this target."""
        self._circuit_open_until = time.monotonic() + self._cfg.circuit_cooldown_seconds
        logger.error(
            f"[{self.source_name}] CIRCUIT BREAKER OPEN: "
            f"{self._consecutive_errors} consecutive errors. "
            f"Cooldown for {self._cfg.circuit_cooldown_seconds:.0f}s"
        )

    def reset_circuit(self):
        """Manually reset the circuit breaker (e.g. after token refresh)."""
        self._circuit_open_until = 0.0
        self._consecutive_errors = 0
        self._consecutive_successes = 0
        self._current_delay = self._cfg.min_delay
        logger.info(f"[{self.source_name}] Circuit breaker manually reset")

    # ── Status & metrics ─────────────────────────────────────────────────

    @property
    def current_delay(self) -> float:
        return self._current_delay

    @property
    def is_circuit_open(self) -> bool:
        if self._circuit_open_until <= 0:
            return False
        return time.monotonic() < self._circuit_open_until

    def get_stats(self) -> dict:
        """Return current rate limiter statistics."""
        return {
            "source": self.source_name,
            "current_delay": round(self._current_delay, 2),
            "consecutive_errors": self._consecutive_errors,
            "consecutive_successes": self._consecutive_successes,
            "circuit_open": self.is_circuit_open,
            "total_requests": self._total_requests,
            "total_errors": self._total_errors,
            "total_rate_limits": self._total_rate_limits,
            "error_rate": (
                round(self._total_errors / self._total_requests, 3)
                if self._total_requests > 0
                else 0.0
            ),
            "tokens_available": round(self._tokens, 1),
            "max_tokens": self._max_tokens if self._max_tokens != float("inf") else None,
        }

    # ── Async context manager ─────────────────────────────────────────────

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.report_failure()
        # Don't suppress exceptions
        return False
