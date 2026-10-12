"""
XunKeXing Collector — API-first, 寻客星 B2B 外贸获客平台

Discovered the internal API at sys.shijiyunhai.cn/api/xkx:
  - get_ft_buyer_list     → company list with phone/email/address
  - get_ft_country_area_list → available countries
  - get_ft_industry_list  → industry categories
  - my                    → user info + auth token

Strategy:
  1. Use Playwright persistent profile ONCE to obtain auth token
  2. Use httpx for ALL data collection (fast, direct JSON)
  3. Token cached in data/xkx_token.json, refreshed when expired

Risk Control (风控):
  - Adaptive rate limiting with jitter (rates configured in config.py)
  - Exponential backoff retry on transient errors (5xx, connection errors)
  - IMMEDIATE stop on auth errors (401/403, token expiry, login required)
  - Circuit breaker: pauses collection after N consecutive errors
  - Response integrity validation (detect captcha/block pages)
  - All risk events logged for audit
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

import config
from collector.rate_limiter import (
    AdaptiveRateLimiter,
    CircuitBreakerOpen,
    RateLimiterConfig,
)
from collector.retry import (
    AuthErrorDetected,
    MaxRetriesExceeded,
    RetryConfig,
    RiskControlError,
    async_retry,
    detect_captcha_or_block,
    validate_api_response,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

API_BASE = "https://sys.shijiyunhai.cn/api/xkx"
DEFAULT_CONFIG_PATH = Path(config.DATA_DIR) / "xunkexing_config.json"
TOKEN_PATH = Path(config.DATA_DIR) / "xkx_token.json"

# Target countries for fastener export (configurable)
# Default: curated set of 24 key markets
DEFAULT_COUNTRY_KEYS = [
    "united_states", "germany", "united_kingdom", "france", "italy",
    "spain", "netherlands", "poland", "canada", "australia",
    "japan", "south_korea", "brazil", "mexico", "turkey",
    "india", "vietnam", "thailand", "indonesia", "malaysia",
    "united_arab_emirates", "saudi_arabia", "south_africa", "russia",
]

# Full country list from API (207 valid countries, filtered from 220)
# Generated from /get_ft_country_area_list — excludes meta-regions and unknowns
ALL_COUNTRY_KEYS = [
    "ethiopia", "singapore", "france", "india", "sweden", "poland",
    "united_states", "united_kingdom", "turkey", "germany", "israel",
    "pakistan", "libyan_arab_jm", "norway", "ireland", "netherlands",
    "hong_kong", "korea_rep.", "italy", "estonia", "saudi_arabia", "ghana",
    "spain", "thailand", "eritrea", "albania", "algeria", "afghanistan",
    "argentina", "australia", "angora", "andorra", "greece", "japan",
    "indonesia", "austria", "azerbaijan", "armenia", "bahamas", "bahrain",
    "bangladesh", "barbados", "belgium", "belize", "benin", "new_zealand",
    "bolivia", "botswana", "brazil", "brunei", "bulgaria", "burkina_faso",
    "aruba", "burundi", "cambodia", "cameroon", "canada", "iran",
    "denmark", "russia", "peru", "ukraine", "kirghizia", "croatia_rep",
    "nepal", "mongolia", "costa_rica", "taiwan", "nigeria", "syrian",
    "s.africa", "malaysia", "kuwait", "philippines", "fiji", "chile",
    "morocco", "egypt", "united_arab_emirates", "finland", "colombia",
    "sri_lanka", "cote_d%e2%80%98lvoir", "trinidad_&_tobago", "oman",
    "republic_of_yemen", "jordan", "switzerland", "sudan", "lebanon",
    "congo", "panama", "qatar", "tanzania", "comoros", "iraq", "china",
    "central_african_rep.", "cuba", "czech_rep", "cyprus", "dominica",
    "hungary", "macau", "nicaragua", "ecuador", "palestine", "djibouti",
    "dominican_rep.", "uzbekstan", "el_salvador", "lithuania", "georgia",
    "kazakhstan", "latvia", "vietnam", "mexico", "myanmar", "romania",
    "puerto_rico", "zimbabwe", "gambia", "gabon", "papua_new_guinea",
    "jamaica", "mauritius", "south_korea", "liberia", "luxembourg",
    "macedonia_rep", "malaw", "madagascar", "mali", "malta", "portugal",
    "venezuela", "tunisia", "honduras", "guatemala", "guinea_bissau",
    "mozambique", "kenya", "slovakia", "paraguay", "senegal", "seychelles",
    "iceland", "uruguay", "laos,pdr", "sierra_leone", "reunion", "slovenia",
    "vanuatu", "zambia", "monaco", "new_caledonia", "suriname", "bhutan",
    "maldives", "togo", "uganda", "guinea", "yugoslavia_fr", "eq.guinea",
    "cape_verde", "mauritania", "swaziland", "liechtenstein", "somalia",
    "moldavia", "rwanda", "samoa", "palau", "namibia", "turks_&_caicos_is",
    "san_marino", "niger", "guyana", "tonga", "haiti", "french_guyana",
    "micronesia_fs", "gibraltar", "antigua_&_barbuda", "kiribati",
    "guadeloupe", "lesotho", "turkmenistan", "tadzhikistan", "cayman_is",
    "cook_is", "curacao", "br._virgin_is", "saint_lucia", "solomon_is",
    "canary_is", "marshall_lslands", "chad", "the_west_indies",
    "serbia_and_montenegro", "bosnia_and_herzegovina", "grenada",
    "saint_vincent_and_the_grenadines", "timor_leste", "nauru",
    "sao_tome_and_principe", "tuvalu",
]

# Industry IDs relevant to fasteners (紧固件) — all 21 industries from the API.
# NOTE: The API's industry_id filter appears to be non-functional (always returns
# the same results regardless of industry_id). Use keyword-based search instead
# for targeting fastener buyers. Industry IDs kept for reference/future use.
# Tier 1 (HIGH — direct fastener buyers):
FASTENER_INDUSTRY_IDS = [
    "7",   # Construction Industry (建筑行业)
    "12",  # Industrial Supplies (机械/工业产品)
    "15",  # Others (综合 — may include automotive, machinery, energy)
    "22",  # Default Category
]
# Tier 2 (MEDIUM — significant fastener usage):
FASTENER_INDUSTRY_IDS_MEDIUM = [
    "2",   # Electronics / Electrical (电子电工)
    "3",   # Consumer Electronics (家用电器)
    "5",   # ceramics (陶瓷 — construction materials)
    "10",  # Mineral / Chemicals (矿产化工 — processing equipment)
    "19",  # Telecommunications (通讯产品)
]
# Tier 3 (BROAD — some fastener usage in every industry):
FASTENER_INDUSTRY_IDS_BROAD = [
    "4",   # Food / Beverage (食品饮料)
    "6",   # Crafts/Gifts (工艺品)
    "8",   # Garments / Leathers (纺织皮革)
    "9",   # Medicine / Health / Beauty (医药保健)
    "11",  # daily use (日用品)
    "13",  # office supplies (办公文具)
    "14",  # Jewellery / Ornaments (珠宝首饰)
    "16",  # toy (玩具)
    "17",  # suitcases / leather (箱包皮具)
    "18",  # Native Products (特产)
    "20",  # Garments / Apparels (服装)
    "21",  # sporting good / casual goods (运动休闲)
]

# Fastener-relevant keywords for keyword-based search.
# The API's keyword parameter DOES work (unlike industry_id) and returns
# different results per keyword. This is the PRIMARY scaling mechanism.
# Tier 1: Direct fastener products (highest relevance)
FASTENER_KEYWORDS_TIER1 = [
    "bolt", "screw", "nut", "washer", "fastener", "rivet",
]
# Tier 2: Related hardware and industrial products
FASTENER_KEYWORDS_TIER2 = [
    "hardware", "industrial supply", "metal parts", "anchor",
    "stud", "threaded", "pin", "clamp", "spring", "stamping",
]
# Tier 3: Adjacent industries that use fasteners heavily
FASTENER_KEYWORDS_TIER3 = [
    "construction", "automotive parts", "machinery", "furniture hardware",
    "building material", "plumbing", "electrical supply", "steel",
]
# Tier 4: Long-tail fastener sub-categories — added 2026-07-30 to expand
# coverage after Tier 1-3 keywords saturated (~16% new/found rate).
# These specific product terms surface different buyer segments than the
# broad Tier 1-2 keywords and should each return distinct result sets.
FASTENER_KEYWORDS_TIER4 = [
    "self tapping screw", "hex bolt", "lock nut", "flat washer",
    "wood screw", "machine screw", "drywall screw", "socket head",
    "threaded rod", "stainless steel fastener", "hex nut",
    "spring washer", "countersunk screw", "eye bolt", "u bolt",
]

# XunKeXing-specific auth error codes (discovered from API behavior)
XKX_AUTH_ERROR_CODES = {
    -1,    # generic error (often auth-related)
    401,   # HTTP unauthorized
    403,   # HTTP forbidden
    1001,  # common auth error code pattern
    1002,
}

# API response `msg` keywords that indicate auth/risk problems
XKX_RISK_KEYWORDS = [
    "登录", "login", "sign in", "signin",
    "token", "过期", "expired", "expire",
    "未授权", "unauthorized", "认证失败", "auth",
    "重新登录", "relogin",
    "账号", "account", "禁用", "disabled",
    "密码", "password", "凭据", "credential",
    "频繁", "frequent", "too many", "limit",
    "异常", "abnormal", "suspicious",
]


class XunKeXingCollector:
    """Collect leads from XunKeXing (寻客星) via direct API calls.

    API-first approach — Playwright is only used for initial token acquisition.

    Risk control features:
    - Adaptive rate limiting with token bucket + jitter
    - Exponential backoff retry on transient failures
    - Auth error detection → immediate stop (no bypass attempt)
    - Circuit breaker → pause after consecutive errors
    - Response integrity validation
    """

    source_name = "xunkexing"

    def __init__(
        self,
        config_path: Optional[Path] = None,
        country_keys: Optional[list[str]] = None,
        max_per_country: int = 100,
        headless: bool = True,
        # Keyword search: list of keywords to query (each yields different results)
        # None/empty = no keyword filter (query all), which should always be included
        keywords: Optional[list[str]] = None,
        # Include a no-keyword (all results) query per country
        include_unfiltered: bool = True,
        # Rate limiter config (override env vars)
        requests_per_minute: Optional[int] = None,
        min_delay: Optional[float] = None,
        # Scaling: use full country list (207) instead of default 24
        use_all_countries: bool = False,
        # Scaling: use fastener keyword tiers for maximum coverage
        use_keyword_search: bool = False,
        # DEPRECATED: industry_id filter doesn't work on this API (kept for compat)
        industry_ids: Optional[list[str]] = None,
        use_industry_tiers: bool = False,
    ):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self._cfg = self._load_config()
        # Country selection priority: explicit arg > config flag > config file > code default
        if country_keys:
            self.country_keys = country_keys
        elif use_all_countries:
            self.country_keys = ALL_COUNTRY_KEYS
        else:
            self.country_keys = self._cfg.get(
                "country_keys", DEFAULT_COUNTRY_KEYS
            )
        self.max_per_country = max_per_country
        self.headless = headless

        # Build keyword list
        if use_keyword_search:
            self.keywords = (
                FASTENER_KEYWORDS_TIER1
                + FASTENER_KEYWORDS_TIER2
                + FASTENER_KEYWORDS_TIER3
                + FASTENER_KEYWORDS_TIER4
            )
        else:
            self.keywords = keywords or []

        self.include_unfiltered = include_unfiltered

        # Track progress across the entire country×keyword scan
        self._total_combos: int = 0
        self._completed_combos: int = 0

        # Rate limiter
        rl_config = RateLimiterConfig(
            requests_per_minute=requests_per_minute or config.RATE_LIMIT_REQUESTS_PER_MINUTE,
            min_delay=min_delay or config.RATE_LIMIT_MIN_DELAY,
            max_delay=config.RATE_LIMIT_MAX_DELAY,
            jitter_factor=config.RATE_LIMIT_JITTER,
            backoff_multiplier=config.RATE_LIMIT_BACKOFF_MULTIPLIER,
            max_consecutive_errors=config.RATE_LIMIT_MAX_CONSECUTIVE_ERRORS,
            circuit_cooldown_seconds=config.RATE_LIMIT_CIRCUIT_COOLDOWN,
        )
        self._rate_limiter = AdaptiveRateLimiter(rl_config, source_name="xunkexing")

        # Retry config
        self._retry_config = RetryConfig(
            max_retries=config.RETRY_MAX_ATTEMPTS,
            base_delay=config.RETRY_BASE_DELAY,
            max_delay=config.RETRY_MAX_DELAY,
            backoff_factor=config.RETRY_BACKOFF_FACTOR,
        )

        self._token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

        # Per-country results (populated during collect(), read by scheduler)
        self._country_results: dict[str, dict] = {}

        # Token status for diagnostics (read by scheduler for logging)
        self._token_status: dict = {"source": "unknown", "valid": None, "detail": ""}

    # ── Main entry point ─────────────────────────────────────────────────

    async def collect(self) -> list[dict]:
        """Collect leads from all configured countries via API.

        Two search dimensions (keyword search is the PRIMARY scaling mechanism):
        1. Unfiltered: no keyword, gets all results per country (sorted by platform default)
        2. Keyword-filtered: specific keywords (bolt, screw, etc.) surface DIFFERENT
           subsets of buyers within the same country

        The API's industry_id filter is non-functional (returns identical results
        regardless of industry). Keyword search IS functional and returns
        distinct result sets per keyword.

        Risk control flow:
        1. Ensure valid token (refresh if needed)
        2. Create rate-limited HTTP client
        3. For each country×keyword combo: fetch with retry, stop on auth error
        4. Circuit breaker prevents hammering after consecutive failures
        """
        # Step 1: Get auth token
        await self._ensure_token()

        # Step 2: Create API client with token header
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "token": self._token,
                    "User-Agent": config.SCRAPE_USER_AGENT,
                },
                timeout=30.0,
                base_url=API_BASE,
            )

        # Step 3: Build the work list (country × keyword combinations)
        # Always include one unfiltered query per country
        queries_per_country: list[str] = []
        if self.include_unfiltered:
            queries_per_country.append("")  # empty = no keyword filter
        for kw in self.keywords:
            queries_per_country.append(kw)

        work_items: list[tuple[str, str]] = []
        for country_key in self.country_keys:
            for keyword in queries_per_country:
                work_items.append((country_key, keyword))

        self._total_combos = len(work_items)
        self._completed_combos = 0

        logger.info(
            f"Collection plan: {len(self.country_keys)} countries × "
            f"{len(queries_per_country)} keywords = {self._total_combos} queries"
        )

        # Step 4: Collect from each country×keyword combo, with risk control
        all_leads: list[dict] = []
        seen_companies: set[str] = set()
        auth_error_occurred = False
        self._country_results = {}  # Reset per-country tracking

        # Per-country aggregation (merge keyword results)
        country_leads: dict[str, list] = {ck: [] for ck in self.country_keys}

        for country_key, keyword in work_items:
            if auth_error_occurred:
                break

            self._completed_combos += 1
            kw_label = keyword if keyword else "ALL"
            combo_label = f"{country_key}/{kw_label}"
            progress_pct = self._completed_combos / self._total_combos * 100

            logger.info(
                f"[{self._completed_combos}/{self._total_combos} "
                f"({progress_pct:.0f}%)] {combo_label}"
            )

            try:
                leads = await self._fetch_country_leads(
                    country_key, keyword=keyword
                )
                new_for_combo = 0
                for lead in leads:
                    name_key = (lead.get("company_name") or "").strip().lower()
                    if name_key and name_key not in seen_companies:
                        seen_companies.add(name_key)
                        all_leads.append(lead)
                        country_leads[country_key].append(lead)
                        new_for_combo += 1

                logger.info(
                    f"  {combo_label}: {len(leads)} fetched, {new_for_combo} new "
                    f"(total unique: {len(all_leads)}, "
                    f"delay: {self._rate_limiter.current_delay:.1f}s)"
                )

                # Track per-country results (merge across keywords)
                if country_key not in self._country_results:
                    self._country_results[country_key] = {
                        "success": True, "leads_found": 0, "leads_new": 0,
                    }
                ckr = self._country_results[country_key]
                ckr["leads_found"] += len(leads)
                ckr["leads_new"] += new_for_combo

            except AuthErrorDetected as e:
                logger.error(
                    f"  {combo_label}: AUTH ERROR — stopping all collection. "
                    f"Account may need re-login. Detail: {e.detail}"
                )
                self._country_results[country_key] = {
                    "success": False, "leads_found": 0, "error": "auth_error"
                }
                auth_error_occurred = True
                self._invalidate_token()
                break

            except RiskControlError as e:
                logger.error(
                    f"  {combo_label}: RISK CONTROL — stopping all collection. "
                    f"Reason: {e.reason}"
                )
                if country_key not in self._country_results:
                    self._country_results[country_key] = {
                        "success": False, "leads_found": 0, "error": "risk_control"
                    }
                auth_error_occurred = True
                break

            except CircuitBreakerOpen as e:
                logger.error(
                    f"  {combo_label}: CIRCUIT BREAKER OPEN — "
                    f"pausing for {e.cooldown_remaining:.0f}s"
                )
                if country_key not in self._country_results:
                    self._country_results[country_key] = {
                        "success": False, "leads_found": 0, "error": "circuit_breaker"
                    }
                break

            except MaxRetriesExceeded as e:
                logger.error(
                    f"  {combo_label}: MAX RETRIES EXCEEDED — "
                    f"{e.attempts} attempts failed. Last error: {e.last_error}"
                )
                continue

            except Exception:
                logger.exception(f"  {combo_label}: UNEXPECTED ERROR")
                continue

        # Per-country summary
        countries_with_leads = sum(
            1 for ck in self.country_keys
            if self._country_results.get(ck, {}).get("leads_found", 0) > 0
        )
        logger.info(
            f"Collection complete: {len(all_leads)} unique leads from "
            f"{countries_with_leads}/{len(self.country_keys)} countries "
            f"({self._completed_combos} queries). "
            f"Rate limiter stats: {self._rate_limiter.get_stats()}"
        )

        return all_leads

    async def _fetch_country_leads(
        self, country_key: str, keyword: str = ""
    ) -> list[dict]:
        """Fetch all leads for a country+keyword, deep-paginating to the end.

        The API never returns a total count (verified 2026-08-08) — an EMPTY
        page is the only reliable end-of-data marker. Pages are fetched until an
        empty page, capped by max_per_country leads and a hard page-safety cap.

        The old logic gated on `page * page_size < total`, which was always
        False with total=0, so every query stopped after page 1 and deep data
        was never reached (浅入浅出).
        """
        leads: list[dict] = []
        page = 1
        page_size = 50  # max per request
        max_pages = getattr(config, "COLLECTION_MAX_PAGES_PER_QUERY", 100)

        while len(leads) < self.max_per_country:
            # Build the request as a callable for retry
            page_leads, total, has_more = await self._fetch_page_with_retry(
                country_key, page, page_size, keyword=keyword
            )

            leads.extend(page_leads)

            if not has_more or len(page_leads) == 0:
                break

            page += 1
            if page > max_pages:
                logger.warning(
                    f"  {country_key}/{keyword or 'ALL'}: hit {max_pages}-page "
                    f"safety cap ({len(leads)} leads) — ending pagination early"
                )
                break

        return leads[: self.max_per_country]

    async def _fetch_page_with_retry(
        self, country_key: str, page: int, page_size: int,
        keyword: str = "",
    ) -> tuple[list[dict], int, bool]:
        """Fetch a single page with retry and risk control.

        Returns (leads, total_count, has_more_pages).

        Raises:
            AuthErrorDetected: Auth failure — stop collection.
            RiskControlError: Captcha/block detected — stop collection.
            CircuitBreakerOpen: Circuit is open — stop collection.
            MaxRetriesExceeded: Transient errors exhausted retries.
        """
        kw_short = keyword[:15] if keyword else "all"
        context = f"xkx_{country_key}_kw{kw_short}_p{page}"

        async def do_request():
            return await self._client.get(
                "/get_ft_buyer_list",
                params={
                    "page": page,
                    "page_size": page_size,
                    "country_keys": country_key,
                    "keyword": keyword,
                    "industry_id": "",
                },
            )

        # Execute with retry + rate limiting
        response = await async_retry(
            do_request,
            config=self._retry_config,
            rate_limiter=self._rate_limiter,
            context=context,
            response_validator=detect_captcha_or_block,
            auth_error_codes=XKX_AUTH_ERROR_CODES,
            risk_keywords=XKX_RISK_KEYWORDS,
        )

        # Parse response
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 1:
            msg = data.get("msg", "unknown error")
            code = data.get("code")

            # Check for auth/risk signals in error response
            risk_reason = validate_api_response(
                data, source=context,
                auth_error_codes=XKX_AUTH_ERROR_CODES,
                risk_keywords=XKX_RISK_KEYWORDS,
            )
            if risk_reason:
                is_auth = any(
                    kw in risk_reason.lower()
                    for kw in ["auth", "token", "login", "登录", "expired", "过期"]
                )
                if is_auth:
                    raise AuthErrorDetected(self.source_name, risk_reason, data)
                else:
                    logger.warning(f"[{context}] Risk signal: {risk_reason}")

            logger.warning(f"[{context}] API returned code={code}: {msg}")
            return [], 0, False

        items = data.get("data", [])
        total = data.get("total", 0)  # API never returns it (always 0) — kept for logging only

        # Map API items to lead dicts
        page_leads = []
        for item in items:
            lead = self._map_api_to_lead(item, country_key)
            if lead:
                page_leads.append(lead)

        # NOTE: The API never returns a 'total' field (verified 2026-08-08 —
        # response keys are only code/msg/time/data). An empty page is the only
        # reliable end-of-data marker. Previously `has_more` was computed as
        # `page * page_size < total` which was always False with total=0, so
        # every query stopped after page 1 and never reached deep data.
        has_more = len(items) > 0

        return page_leads, total, has_more

    # ── Data mapping ──────────────────────────────────────────────────────

    def _map_api_to_lead(self, item: dict, country_key: str) -> Optional[dict]:
        """Map an API buyer record to a Lead-compatible dict."""
        company_name = (item.get("company_name") or "").strip()
        email = (item.get("email") or "").strip()
        phone = (item.get("telephone") or "").strip()
        country = (item.get("country_area") or "").strip()
        category = (item.get("category_name") or "").strip()
        address = (item.get("address") or "").strip()
        website = (item.get("website") or "").strip()
        fax = (item.get("fax") or "").strip()

        # Skip records without at least a company name
        if not company_name:
            return None

        # Build source_detail with extra metadata
        source_detail = {
            "platform": "xunkexing",
            "country_key": country_key,
            "category": category,
            "address": address,
            "fax": fax,
            "buyer_id": item.get("id"),
        }

        return {
            "company_name": company_name,
            "contact_name": (item.get("contact_person") or "").strip() or None,
            "email": email if email else None,
            "phone": phone if phone else None,
            "whatsapp": None,  # not provided by API
            "website": website if website else None,
            "country": self._normalize_country(country),
            "industry": category if category else "fasteners",
            "source_url": (
                f"https://sys.shijiyunhai.cn/xkxpc/#/home/search_foreign"
                f"?country_keys={country_key}"
            ),
            "source_detail": json.dumps(source_detail, ensure_ascii=False),
        }

    @staticmethod
    def _normalize_country(raw: str) -> Optional[str]:
        """Extract English country name from combined field like 'United States (美国)'."""
        if not raw:
            return None
        # Remove Chinese parenthetical
        cleaned = re.sub(r"\s*\(.*?\)\s*", "", raw).strip()
        return cleaned if cleaned else raw

    # ── Token management ──────────────────────────────────────────────────

    async def _ensure_token(self):
        """Get a valid auth token, refreshing if needed.

        Raises AuthErrorDetected if token cannot be obtained.
        """
        # Try cached token first
        cached = self._load_cached_token()
        if cached:
            expire_str = cached.get("expire_time", "unknown")
            cache_date = cached.get("cached_at", "unknown")
            if self._token_is_valid(cached):
                self._token = cached["token"]
                self._token_status = {
                    "source": "cached",
                    "valid": True,
                    "detail": f"expires {expire_str}",
                }
                logger.info(
                    f"Using cached API token (expires: {expire_str}, "
                    f"cached: {cache_date})"
                )
                return
            else:
                self._token_status = {
                    "source": "cached",
                    "valid": False,
                    "detail": f"expired at {expire_str}",
                }
                logger.warning(
                    f"Cached token expired or invalid "
                    f"(expire_time={expire_str}, cached_at={cache_date}) — "
                    f"refreshing via browser"
                )
        else:
            self._token_status = {
                "source": "none",
                "valid": None,
                "detail": "no cached token",
            }

        # Refresh via Playwright browser
        logger.info("Refreshing API token via browser...")
        try:
            self._token = await self._fetch_token_via_browser()
            # Cache it
            expire_at = self._save_cached_token(self._token)
            self._token_status = {
                "source": "browser_refresh",
                "valid": True,
                "detail": f"new expiry {expire_at}",
            }
            logger.info(
                f"Token refreshed and cached (new expiry: {expire_at})"
            )
        except Exception as e:
            self._token_status = {
                "source": "browser_refresh",
                "valid": False,
                "detail": f"refresh failed: {e}",
            }
            logger.error(
                f"Failed to refresh token via browser: {e}\n"
                f"  Profile dir: {Path(config.DATA_DIR) / 'xkx_profile'}\n"
                f"  Headless mode: {self.headless}\n"
                f"  HINT: Try running 'python -m collector.setup xunkexing' "
                f"to manually re-login"
            )
            raise AuthErrorDetected(
                self.source_name,
                f"Could not obtain auth token: {e}",
            ) from e

    async def _fetch_token_via_browser(self) -> str:
        """Launch Playwright to get a fresh auth token from the SPA."""
        from collector.browser import BrowserManager

        profile_dir = Path(config.DATA_DIR) / "xkx_profile"

        async with BrowserManager(
            headless=self.headless,
            profile_dir=profile_dir,
        ) as bm:
            page = await bm.new_page()
            try:
                await page.goto(
                    "https://sys.shijiyunhai.cn/xkxpc/#/home",
                    wait_until="networkidle",
                    timeout=30000,
                )
                await asyncio.sleep(2)

                # Extract token from the SPA's localStorage
                token = await page.evaluate(
                    "() => {"
                    "  const ui = localStorage.getItem('userInfo');"
                    "  if (ui) {"
                    "    try { return JSON.parse(ui).token; } catch(e) {}"
                    "  }"
                    "  return null;"
                    "}"
                )

                if token:
                    return token

                # Fallback: couldn't extract token — capture screenshot
                # to diagnose whether we're seeing a login page, captcha,
                # or logged-in dashboard
                try:
                    screenshot_path = (
                        Path(config.DATA_DIR) / "xkx_token_failure.png"
                    )
                    await page.screenshot(path=str(screenshot_path))
                    logger.error(
                        f"Token extraction failed. "
                        f"Screenshot saved to {screenshot_path}"
                    )
                except Exception:
                    pass

                raise RuntimeError(
                    "Could not extract token from browser. "
                    "Session may have expired. "
                    "Please run: python -m collector.setup xunkexing"
                )
            finally:
                await page.close()

    @staticmethod
    def _token_is_valid(token_data: dict) -> bool:
        """Check if cached token is still valid."""
        expire_str = token_data.get("expire_time")
        if not expire_str:
            return False
        try:
            expire = datetime.fromisoformat(expire_str)
            # Consider expired 1 hour before actual expiry
            return datetime.now(timezone.utc) < expire
        except (ValueError, TypeError):
            return False

    def _load_cached_token(self) -> Optional[dict]:
        """Load cached token from disk."""
        if not TOKEN_PATH.exists():
            return None
        try:
            with open(TOKEN_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_cached_token(self, token: str) -> str:
        """Save token to disk with a conservative expiry guess.

        Returns the expiry time as an ISO format string.
        """
        from datetime import timedelta
        expire = datetime.now(timezone.utc) + timedelta(days=7)
        expire_str = expire.isoformat()
        data = {
            "token": token,
            "expire_time": expire_str,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_PATH, "w") as f:
            json.dump(data, f)
        return expire_str

    def _invalidate_token(self):
        """Delete cached token — forces re-login on next run."""
        if TOKEN_PATH.exists():
            TOKEN_PATH.unlink()
            logger.warning("Cached token invalidated — will require re-login")

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def close(self):
        """Release HTTP client resources."""
        if self._client:
            try:
                await self._client.aclose()
            except RuntimeError as e:
                if "Event loop is closed" in str(e):
                    # Windows ProactorEventLoop edge case: the event loop
                    # has already been torn down. The HTTP transport's
                    # pending close-callbacks cannot execute — drop the
                    # client reference so GC handles cleanup.
                    logger.debug(
                        "HTTP client close deferred: event loop already shut down"
                    )
                else:
                    raise
            finally:
                self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ── Config ─────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        """Load configuration from JSON file."""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logger.warning(f"Failed to load config from {self.config_path}")
        return {}
