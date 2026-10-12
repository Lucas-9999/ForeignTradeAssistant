"""
Retry logic with exponential backoff, auth error detection, and risk control.

Key behaviors:
1. Exponential backoff + jitter on transient failures
2. IMMEDIATE stop on auth errors (no retry — triggers re-login flow)
3. Response integrity validation (detect captcha/block pages)
4. Configurable retryable vs. fatal status codes

Usage:
    from collector.retry import async_retry, AuthErrorDetected, RiskControlError

    try:
        result = await async_retry(
            lambda: client.get("/api/data"),
            config=RetryConfig(max_retries=3),
            rate_limiter=limiter,
            context="fetch_leads",
        )
    except AuthErrorDetected:
        logger.error("Token expired — needs re-login")
        raise  # propagate to stop collection
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════════════


class AuthErrorDetected(Exception):
    """API indicates the session/token is invalid — STOP, do NOT retry.

    This is NOT a transient error. The collector should:
    1. Stop all ongoing collection immediately
    2. Log the event for review
    3. NOT attempt to bypass authentication
    """

    def __init__(self, source: str, detail: str = "", response_data: Any = None):
        self.source = source
        self.detail = detail
        self.response_data = response_data
        msg = f"[{source}] Auth error detected"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class RiskControlError(Exception):
    """API indicates a risk control action — account flagged, captcha, etc.

    This is NOT a transient error. The collector should:
    1. Stop all ongoing collection
    2. NOT attempt to bypass the check
    3. Alert the operator to manually review
    """

    def __init__(self, source: str, reason: str = "", evidence: Any = None):
        self.source = source
        self.reason = reason
        self.evidence = evidence
        msg = f"[{source}] Risk control triggered"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


class MaxRetriesExceeded(Exception):
    """All retry attempts exhausted on a transient error."""

    def __init__(self, context: str, attempts: int, last_error: Optional[Exception] = None):
        self.context = context
        self.attempts = attempts
        self.last_error = last_error
        msg = f"[{context}] Max retries ({attempts}) exceeded"
        if last_error:
            msg += f": {type(last_error).__name__}: {last_error}"
        super().__init__(msg)


# ═══════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    # Maximum number of retry attempts (total tries = 1 + max_retries)
    max_retries: int = 3

    # Base delay before first retry (seconds)
    base_delay: float = 2.0

    # Maximum delay between retries (seconds)
    max_delay: float = 60.0

    # Exponential backoff multiplier (delay *= backoff_factor each retry)
    backoff_factor: float = 2.0

    # Add random jitter to retry delays
    jitter: bool = True

    # Jitter range: delay * (1 +/- jitter_factor)
    jitter_factor: float = 0.25

    # HTTP status codes that are retryable (transient errors)
    retryable_statuses: set[int] = field(default_factory=lambda: {
        429,  # Too Many Requests
        500,  # Internal Server Error
        502,  # Bad Gateway
        503,  # Service Unavailable
        504,  # Gateway Timeout
    })

    # HTTP status codes that indicate auth failure (fatal — stop immediately)
    auth_error_statuses: set[int] = field(default_factory=lambda: {
        401,  # Unauthorized
        403,  # Forbidden (may also be IP block)
    })

    # Connection-level errors that are retryable
    retryable_exceptions: tuple = (
        asyncio.TimeoutError,
        ConnectionError,
        OSError,  # includes ConnectionRefused, ConnectionReset
    )


# ═══════════════════════════════════════════════════════════════════════════
# Response validation
# ═══════════════════════════════════════════════════════════════════════════


def detect_captcha_or_block(
    response_text: str,
    content_type: str = "",
) -> Optional[str]:
    """Check if an HTTP response looks like a captcha or block page.

    Returns a human-readable reason string if a block is detected, None otherwise.
    This is a heuristic check — not exhaustive but catches common patterns.
    """
    text_lower = response_text[:2000].lower()  # check first 2K chars

    # If content-type claims to be JSON but body is HTML, that's suspicious
    if "application/json" in content_type:
        if response_text.strip().startswith("<!DOCTYPE") or response_text.strip().startswith("<html"):
            return "JSON content-type but body is HTML (possible redirect/block)"

    # Common captcha/block indicators in page content
    captcha_indicators = [
        ("captcha", "page contains 'captcha'"),
        ("verify you are a human", "human verification prompt"),
        ("please verify", "verification requested"),
        ("access denied", "access denied"),
        ("your request has been blocked", "request blocked"),
        ("cf-browser-verify", "Cloudflare browser check"),
        ("_cf_chl_opt", "Cloudflare challenge"),
        ("please enable javascript", "JavaScript required (anti-bot)"),
        ("banned", "banned indicator"),
        ("ip has been", "IP restriction"),
        ("too many requests", "rate limit page"),
        ("请求过于频繁", "rate limit (CN)"),
        ("访问被拒绝", "access denied (CN)"),
        ("验证码", "captcha (CN)"),
        ("人机验证", "human verification (CN)"),
    ]

    for pattern, reason in captcha_indicators:
        if pattern in text_lower:
            return reason

    return None


def validate_api_response(
    response_data: Any,
    source: str = "unknown",
    auth_error_codes: Optional[set] = None,
    risk_keywords: Optional[list[str]] = None,
) -> Optional[str]:
    """Validate an API JSON response for auth errors or risk signals.

    Returns a reason string if a problem is detected, None if the response is clean.

    Args:
        response_data: Parsed JSON response (dict expected).
        source: Source name for error messages.
        auth_error_codes: API-specific error codes that mean auth failure.
        risk_keywords: Keywords in error messages that indicate auth/risk issues.
    """
    if not isinstance(response_data, dict):
        return None

    code = response_data.get("code")
    msg = str(response_data.get("msg", "")).lower()

    # Default auth error codes
    if auth_error_codes is None:
        auth_error_codes = {-1, 401, 403, 1001, 1002, 1003}

    # Default risk/auth keywords in Chinese and English
    if risk_keywords is None:
        risk_keywords = [
            "登录", "login", "token", "过期", "expired", "expire",
            "未授权", "unauthorized", "认证", "auth", "验证",
            "重新登录", "relogin", "sign in", "signin",
            "密码", "password", "凭据", "credential",
            "账号", "account", "被封", "disabled", "frozen",
        ]

    # Check error codes
    if code is not None and code in auth_error_codes:
        return f"API error code {code}: {msg}"

    # Check message for auth/risk keywords
    for keyword in risk_keywords:
        if keyword in msg:
            return f"API message contains '{keyword}': {msg}"

    # Check for explicit auth failure fields
    if response_data.get("auth_error") or response_data.get("need_login"):
        return "Response indicates auth failure"

    return None


# ═══════════════════════════════════════════════════════════════════════════
# Core retry function
# ═══════════════════════════════════════════════════════════════════════════


async def async_retry(
    func: Callable[[], Awaitable[Any]],
    *,
    config: Optional[RetryConfig] = None,
    rate_limiter: Any = None,  # AdaptiveRateLimiter, optional
    context: str = "request",
    # Response validation
    response_validator: Optional[Callable[[Any], Optional[str]]] = None,
    # Auth error detection in response body
    auth_error_codes: Optional[set] = None,
    risk_keywords: Optional[list[str]] = None,
) -> Any:
    """Execute an async function with retry logic and risk control.

    Retry behavior:
    - Transient HTTP errors (5xx, 429): exponential backoff + jitter
    - Connection errors (Timeout, ConnectionError): exponential backoff + jitter
    - Auth errors (401, 403, or detected in response): raise AuthErrorDetected IMMEDIATELY
    - Captcha/block detected: raise RiskControlError IMMEDIATELY
    - Non-retryable 4xx (400, 404, etc.): raise immediately

    Args:
        func: Async callable to execute (typically a lambda wrapping an HTTP request).
        config: Retry configuration.
        rate_limiter: Optional AdaptiveRateLimiter to report outcomes to.
        context: Human-readable context for error messages (e.g. "fetch_leads_page1").
        response_validator: Optional function(response_text, content_type) -> str|None
            that checks for captcha/block pages.
        auth_error_codes: API-specific error codes that mean auth failure.
        risk_keywords: Keywords in API error messages indicating auth/risk issues.

    Returns:
        The result of func() on success.

    Raises:
        AuthErrorDetected: Auth failure detected — stop collection.
        RiskControlError: Captcha/block/risk control detected — stop collection.
        MaxRetriesExceeded: All retries exhausted on transient errors.
        httpx.HTTPStatusError: Non-retryable HTTP errors (4xx except 429).
    """
    import httpx

    cfg = config or RetryConfig()
    last_error: Optional[Exception] = None

    for attempt in range(cfg.max_retries + 1):
        try:
            # Acquire rate limit slot before request
            if rate_limiter is not None:
                await rate_limiter.acquire()

            # Execute the request
            result = await func()

            # Success: validate the response
            if response_validator is not None and hasattr(result, "text") and hasattr(result, "headers"):
                content_type = result.headers.get("content-type", "")
                # Only validate text responses
                if "text/html" in content_type or "application/json" in content_type:
                    block_reason = response_validator(result.text, content_type)
                    if block_reason:
                        raise RiskControlError(context, block_reason)

            # Check JSON responses for auth errors
            if auth_error_codes is not None or risk_keywords is not None:
                if hasattr(result, "json"):
                    try:
                        data = result.json() if callable(result.json) else result.json
                    except Exception:
                        data = None
                    if isinstance(data, dict):
                        reason = validate_api_response(
                            data, source=context,
                            auth_error_codes=auth_error_codes,
                            risk_keywords=risk_keywords,
                        )
                        if reason:
                            # Check if it's auth-related
                            is_auth = any(
                                kw in reason.lower()
                                for kw in ["auth", "token", "login", "登录", "expired", "过期", "credential"]
                            )
                            if is_auth:
                                raise AuthErrorDetected(context, reason, data)
                            else:
                                logger.warning(f"[{context}] Risk signal in response: {reason}")

            # Report success to rate limiter
            if rate_limiter is not None:
                rate_limiter.report_success()

            return result

        except (AuthErrorDetected, RiskControlError):
            # Fatal — don't retry, propagate immediately
            raise

        except httpx.HTTPStatusError as e:
            status = e.response.status_code

            # Auth errors — fatal, stop immediately
            if status in cfg.auth_error_statuses:
                logger.error(
                    f"[{context}] Auth error HTTP {status} — stopping collection"
                )
                raise AuthErrorDetected(
                    context,
                    f"HTTP {status} from server",
                    None,
                ) from e

            # 429 Rate Limit — special handling
            if status == 429:
                retry_after = _parse_retry_after(e.response.headers)
                logger.warning(
                    f"[{context}] Rate limited (429), retry_after={retry_after}s, "
                    f"attempt={attempt + 1}/{cfg.max_retries + 1}"
                )
                if rate_limiter is not None:
                    rate_limiter.report_rate_limited(retry_after)

                if attempt < cfg.max_retries:
                    delay = retry_after if retry_after and retry_after > 0 else _compute_delay(cfg, attempt)
                    await asyncio.sleep(delay)
                    last_error = e
                    continue
                else:
                    raise MaxRetriesExceeded(context, cfg.max_retries, e) from e

            # Other retryable statuses (5xx)
            if status in cfg.retryable_statuses:
                logger.warning(
                    f"[{context}] Retryable HTTP {status}, "
                    f"attempt={attempt + 1}/{cfg.max_retries + 1}"
                )
                if rate_limiter is not None:
                    rate_limiter.report_failure(status)

                if attempt < cfg.max_retries:
                    await asyncio.sleep(_compute_delay(cfg, attempt))
                    last_error = e
                    continue
                else:
                    raise MaxRetriesExceeded(context, cfg.max_retries, e) from e

            # Non-retryable 4xx — raise immediately
            logger.error(f"[{context}] Non-retryable HTTP {status}")
            if rate_limiter is not None:
                rate_limiter.report_failure(status)
            raise

        except cfg.retryable_exceptions as e:
            logger.warning(
                f"[{context}] Connection error: {type(e).__name__}: {e}, "
                f"attempt={attempt + 1}/{cfg.max_retries + 1}"
            )
            if rate_limiter is not None:
                rate_limiter.report_failure()

            if attempt < cfg.max_retries:
                await asyncio.sleep(_compute_delay(cfg, attempt))
                last_error = e
                continue
            else:
                raise MaxRetriesExceeded(context, cfg.max_retries, e) from e

        except Exception:
            # Unknown errors — report failure but don't retry
            if rate_limiter is not None:
                rate_limiter.report_failure()
            raise

    # Should not reach here, but just in case
    raise MaxRetriesExceeded(context, cfg.max_retries, last_error)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _compute_delay(cfg: RetryConfig, attempt: int) -> float:
    """Compute the retry delay for a given attempt number (0-indexed)."""
    delay = cfg.base_delay * (cfg.backoff_factor ** attempt)
    delay = min(delay, cfg.max_delay)

    if cfg.jitter:
        jitter_range = delay * cfg.jitter_factor
        delay = delay + random.uniform(-jitter_range, jitter_range)

    return max(0, delay)


def _parse_retry_after(headers) -> Optional[float]:
    """Extract Retry-After seconds from HTTP response headers."""
    import httpx
    if not isinstance(headers, (httpx.Headers, dict)):
        return None

    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if retry_after is None:
        return None

    try:
        return float(retry_after)
    except (ValueError, TypeError):
        # Could be a date string — ignore for simplicity
        return None
