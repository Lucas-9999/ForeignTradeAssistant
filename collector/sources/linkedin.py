"""
LinkedIn Collector — B2B company & contact sourcing for fastener industry.

Strategy:
  - Use Playwright with persistent profile (login once, session persists)
  - Search LinkedIn for fastener companies and key decision-makers
  - Extract company pages: name, website, industry, size, country
  - Extract employee profiles: name, title → used for email format inference
  - Target: linkedin.com (full site, authenticated)

Risk Control (LinkedIn is extremely sensitive to automation):
  - Ultra-conservative rate limits: 5 RPM, 30s min delay
  - Exponential backoff on transient errors (up to 5 min)
  - Auth error detection: immediate stop on login/security challenges
  - Circuit breaker on 4 consecutive failures (30 min cooldown)
  - Response integrity checks (detect security verification pages)
  - Human-like scrolling and delays between actions

Data Sources:
  1. Company search: "fasteners manufacturer", "screws bolts supplier", etc.
  2. People search: "fastener purchasing manager", "screw export", etc.
  3. Company detail pages: extract website, industry, employee count
  4. Employee profiles: extract names/titles of key contacts

Note: LinkedIn's ToS restricts automated scraping. This collector:
  - Only accesses data visible to the authenticated account
  - Uses conservative timing that mimics human browsing
  - Requires a real LinkedIn account (use a dedicated one)
  - Respects rate limits to avoid detection
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

import config
from collector.base import BaseCollector
from collector.rate_limiter import (
    AdaptiveRateLimiter,
    CircuitBreakerOpen,
    RateLimiterConfig,
)
from collector.retry import (
    AuthErrorDetected,
    MaxRetriesExceeded,
    RiskControlError,
    RetryConfig,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

LI_BASE = "https://www.linkedin.com"
LI_PROFILE_DIR = Path(config.DATA_DIR) / "linkedin_profile"

# Company search queries (fastener industry, multi-language)
LI_COMPANY_QUERIES = [
    "fasteners manufacturer",
    "screws bolts supplier",
    "industrial fasteners wholesale",
    "fastener distributor",
    "五金紧固件制造",
    "fasteners export company",
    "stainless steel fasteners",
    "automotive fasteners supplier",
]

# People search queries (target decision-makers)
LI_PEOPLE_QUERIES = [
    "fasteners purchasing manager",
    "fastener import export",
    "screw bolt sales director",
    "industrial supply procurement",
    "紧固件外贸经理",
    "fastener business development",
]

# Job titles that indicate decision-making authority
DECISION_MAKER_TITLES = [
    "purchasing", "procurement", "sourcing", "buyer",
    "manager", "director", "president", "ceo", "owner",
    "sales", "export", "import", "business development",
    "supply chain", "general manager", "vice president",
    "采购", "经理", "总监", "外贸",
]

# LinkedIn auth/security indicators in page text
LI_AUTH_INDICATORS = [
    "sign in", "join now", "login",
    "security verification", "verify your identity",
    "unusual activity", "checkpoint",
    "confirm your email", "two-step verification",
    "let's do a quick security check",
]

# Email regex for extracting from company pages
EMAIL_RE = re.compile(
    r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
    re.IGNORECASE,
)

# Website regex (from company about sections)
WEBSITE_RE = re.compile(
    r'(?:https?://)?(?:www\.)?[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s"\'<>\]]*)?',
)


class LinkedInCollector(BaseCollector):
    """Collect fastener industry leads from LinkedIn.

    Usage:
        # First run: headless=False to log in manually
        collector = LinkedInCollector(headless=False, max_companies=30)
        leads = await collector.collect()

        # Subsequent runs: headless=True (session persists)
        collector = LinkedInCollector(headless=True, max_companies=30)
        leads = await collector.collect()
    """

    source_name = "linkedin"

    def __init__(
        self,
        headless: bool = True,
        max_companies: int = 50,
        max_per_query: int = 15,
        profile_dir: Optional[Path] = None,
        company_queries: Optional[list[str]] = None,
        people_queries: Optional[list[str]] = None,
    ):
        super().__init__(
            delay=config.SCRAPE_REQUEST_DELAY * 5,  # LinkedIn needs very slow pacing
            timeout=60.0,
        )
        self.headless = headless
        self.max_companies = max_companies
        self.max_per_query = max_per_query
        self.profile_dir = profile_dir or LI_PROFILE_DIR
        self.company_queries = company_queries or LI_COMPANY_QUERIES
        self.people_queries = people_queries or LI_PEOPLE_QUERIES

        # Rate limiter (ultra-conservative for LinkedIn)
        rl_config = RateLimiterConfig(
            requests_per_minute=5,         # Very slow — LinkedIn is strict
            min_delay=30.0,                # minimum 30s between page loads
            max_delay=300.0,               # back off to 5 min on errors
            jitter_factor=0.5,             # ±50% jitter for human-like timing
            backoff_multiplier=2.5,
            max_consecutive_errors=4,      # open circuit sooner
            circuit_cooldown_seconds=1800,  # 30 min cooldown
        )
        self._rate_limiter = AdaptiveRateLimiter(rl_config, source_name="linkedin")

        # Retry config
        self._retry_config = RetryConfig(
            max_retries=2,
            base_delay=10.0,
            max_delay=300.0,
            backoff_factor=2.5,
        )

        # Playwright state
        self._playwright = None
        self._browser = None
        self._context = None
        self._setup_done = False

        # Results tracking
        self._visited_urls: set[str] = set()
        self._seen_companies: set[str] = set()

    # ── Main entry point ─────────────────────────────────────────────────

    async def collect(self) -> list[dict]:
        """Run LinkedIn collection across company and people searches.

        Returns list of lead dicts compatible with pipeline.raw_to_lead().
        """
        await self._ensure_browser()

        all_leads: list[dict] = []
        auth_error = False

        # Phase 1: Company search
        logger.info(f"LinkedIn: searching {len(self.company_queries)} company queries")
        for query in self.company_queries:
            if auth_error or len(all_leads) >= self.max_companies:
                break
            try:
                leads = await self._search_companies(query)
                all_leads.extend(leads)
                logger.info(f"  Company '{query}': {len(leads)} leads")
            except AuthErrorDetected:
                if not all_leads:
                    logger.warning(
                        "LinkedIn session not logged in. "
                        "Run first-time setup: python -m collector.setup linkedin"
                    )
                logger.error(f"  Company '{query}': AUTH ERROR — stopping")
                auth_error = True
                break
            except Exception:
                logger.exception(f"  Company '{query}': failed")

        # Phase 2: People search (only if we have capacity)
        if not auth_error and len(all_leads) < self.max_companies:
            logger.info(f"LinkedIn: searching {len(self.people_queries)} people queries")
            for query in self.people_queries:
                if auth_error or len(all_leads) >= self.max_companies:
                    break
                try:
                    leads = await self._search_people(query)
                    all_leads.extend(leads)
                    logger.info(f"  People '{query}': {len(leads)} leads")
                except AuthErrorDetected:
                    logger.error(f"  People '{query}': AUTH ERROR — stopping")
                    auth_error = True
                    break
                except Exception:
                    logger.exception(f"  People '{query}': failed")

        logger.info(
            f"LinkedIn collection complete: {len(all_leads)} leads "
            f"from {len(self.company_queries) + len(self.people_queries)} queries"
        )
        return all_leads

    # ── Company Search ───────────────────────────────────────────────────

    async def _search_companies(self, query: str) -> list[dict]:
        """Search LinkedIn companies and extract company info from results."""
        leads: list[dict] = []
        company_urls: list[str] = []

        # Search URL for companies
        search_url = (
            f"{LI_BASE}/search/results/companies/?"
            f"keywords={quote(query)}&origin=GLOBAL_SEARCH_HEADER"
        )

        try:
            company_urls = await self._crawl_search_results(
                search_url, max_results=self.max_per_query, result_type="company"
            )
        except Exception as e:
            logger.error(f"Error crawling company search: {e}")
            return []

        if not company_urls:
            logger.info(f"  No companies found for '{query}'")
            return []

        # Visit each company page → extract info
        for url in company_urls:
            if len(leads) >= self.max_per_query:
                break
            if url in self._visited_urls:
                continue
            self._visited_urls.add(url)

            try:
                lead = await self._extract_company_info(url)
                if lead:
                    leads.append(lead)
            except AuthErrorDetected:
                raise
            except Exception:
                logger.debug(f"  Error extracting company: {url}")
                continue

        return leads

    # ── People Search ────────────────────────────────────────────────────

    async def _search_people(self, query: str) -> list[dict]:
        """Search LinkedIn people and extract company + contact info from profiles."""
        leads: list[dict] = []
        profile_urls: list[str] = []

        search_url = (
            f"{LI_BASE}/search/results/people/?"
            f"keywords={quote(query)}&origin=GLOBAL_SEARCH_HEADER"
        )

        try:
            profile_urls = await self._crawl_search_results(
                search_url, max_results=self.max_per_query, result_type="people"
            )
        except Exception as e:
            logger.error(f"Error crawling people search: {e}")
            return []

        if not profile_urls:
            logger.info(f"  No people found for '{query}'")
            return []

        for url in profile_urls:
            if len(leads) >= self.max_per_query:
                break
            if url in self._visited_urls:
                continue
            self._visited_urls.add(url)

            try:
                lead = await self._extract_profile_info(url)
                if lead and lead.get("company_name"):
                    leads.append(lead)
            except AuthErrorDetected:
                raise
            except Exception:
                logger.debug(f"  Error extracting profile: {url}")
                continue

        return leads

    # ── Search Results Crawling ──────────────────────────────────────────

    async def _crawl_search_results(
        self, search_url: str, max_results: int, result_type: str = "company"
    ) -> list[str]:
        """Crawl LinkedIn search result pages and collect entity URLs.

        Args:
            search_url: Initial search URL.
            max_results: Maximum number of result URLs to collect.
            result_type: "company" or "people" — determines link pattern.

        Returns:
            List of LinkedIn entity URLs.
        """
        urls: list[str] = []
        current_url = search_url
        page = 1
        max_pages = max(2, max_results // 10)

        async with await self._context.new_page() as li_page:
            while len(urls) < max_results and page <= max_pages:
                await self._rate_limiter.acquire()

                try:
                    await li_page.goto(
                        current_url,
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    # Human-like pause
                    await asyncio.sleep(2.0 + asyncio.get_event_loop().time() % 3.0)

                    content = await li_page.content()

                    # Check for auth/security pages
                    if self._is_blocked_or_auth(content):
                        raise AuthErrorDetected(
                            self.source_name,
                            "LinkedIn security/block page detected",
                        )

                    # Scroll down to load more results (human-like)
                    await li_page.evaluate("window.scrollBy(0, 800)")
                    await asyncio.sleep(1.0)

                    # Re-fetch content after scroll
                    content = await li_page.content()
                    soup = await self._parse_html(content)
                    if soup is None:
                        break

                    # Extract result links
                    if result_type == "company":
                        link_patterns = ["/company/"]
                    else:
                        link_patterns = ["/in/", "/sales/people/"]

                    for a_tag in soup.select("a[href]"):
                        href = (a_tag.get("href") or "").strip()
                        for pattern in link_patterns:
                            if pattern in href:
                                # Clean tracking params
                                clean_url = href.split("?")[0]
                                full_url = urljoin(LI_BASE, clean_url)
                                if full_url not in urls:
                                    urls.append(full_url)
                                break

                    # Find next page button
                    next_url = None
                    for a in soup.select("a[href*='page=']"):
                        aria_label = a.get("aria-label", "").lower()
                        if "next" in aria_label:
                            next_url = urljoin(LI_BASE, a.get("href", ""))
                            break

                    if next_url and next_url != current_url:
                        current_url = next_url
                        page += 1
                    else:
                        break

                except AuthErrorDetected:
                    raise
                except Exception as e:
                    logger.warning(f"  Error on search page {page}: {e}")
                    break

        return urls[:max_results]

    # ── Company Page Extraction ──────────────────────────────────────────

    async def _extract_company_info(self, company_url: str) -> Optional[dict]:
        """Visit a LinkedIn company page and extract business information.

        Extracts:
          - Company name, website, industry, company size, headquarters
          - Company description (for relevance scoring)
        """
        await self._rate_limiter.acquire()

        async with await self._context.new_page() as li_page:
            try:
                # Navigate to About page (richer info)
                about_url = f"{company_url}about/"
                await li_page.goto(
                    about_url,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await asyncio.sleep(1.5 + asyncio.get_event_loop().time() % 2.0)

                content = await li_page.content()

                if self._is_blocked_or_auth(content):
                    raise AuthErrorDetected(
                        self.source_name,
                        "LinkedIn security page on company",
                    )

                soup = await self._parse_html(content)
                if soup is None:
                    return None

                # Extract company name
                company_name = None
                for selector in ["h1", ".org-top-card-summary__title",
                                 "[data-test-id='company-name']"]:
                    el = soup.select_one(selector)
                    if el:
                        company_name = el.get_text(strip=True)
                        break

                if not company_name:
                    return None

                # Dedup
                name_key = company_name.lower()
                if name_key in self._seen_companies:
                    return None
                self._seen_companies.add(name_key)

                # Extract website
                website = None
                for selector in [
                    "a[data-test-id='about-us__website']",
                    "a[href*='company_website']",
                    ".org-about-us-company-module__website a",
                    "dt:has-text('Website') + dd a",
                ]:
                    el = soup.select_one(selector)
                    if el:
                        website = (el.get("href") or "").strip()
                        if website and "linkedin.com" not in website:
                            break
                        website = None

                # Extract industry
                industry = None
                for selector in [
                    ".org-about-us-company-module__industry",
                    "dt:has-text('Industry') + dd",
                    "[data-test-id='about-us__industry']",
                ]:
                    el = soup.select_one(selector)
                    if el:
                        industry = el.get_text(strip=True)
                        break

                # Extract company size
                company_size = None
                for selector in [
                    ".org-about-us-company-module__company-size",
                    "dt:has-text('Company size') + dd",
                ]:
                    el = soup.select_one(selector)
                    if el:
                        company_size = el.get_text(strip=True)
                        break

                # Extract headquarters / country
                country = None
                for selector in [
                    ".org-about-us-company-module__headquarters",
                    "dt:has-text('Headquarters') + dd",
                ]:
                    el = soup.select_one(selector)
                    if el:
                        location_text = el.get_text(strip=True)
                        country = self._extract_country(location_text)
                        break

                # Extract description for relevance
                description = ""
                for selector in [
                    ".org-about-us-company-module__description",
                    "[data-test-id='about-us__description']",
                    "p.break-words",
                ]:
                    el = soup.select_one(selector)
                    if el:
                        description = el.get_text(separator=" ", strip=True)[:1000]
                        break

                # Check relevance to fasteners
                text_lower = (company_name + " " + (industry or "") + " " + description).lower()
                is_relevant = self._is_fastener_relevant(text_lower)

                if not is_relevant and not website:
                    return None

                source_detail = {
                    "platform": "linkedin",
                    "company_url": company_url,
                    "company_size": company_size,
                    "relevance": "direct" if is_relevant else "tangential",
                }

                return {
                    "company_name": company_name,
                    "contact_name": None,
                    "email": None,
                    "phone": None,
                    "whatsapp": None,
                    "website": website,
                    "industry": industry or "fasteners",
                    "country": country,
                    "source_url": company_url,
                    "source_detail": json.dumps(source_detail, ensure_ascii=False),
                    "notes": self._truncate(description, 500),
                }

            except AuthErrorDetected:
                raise
            except Exception as e:
                logger.debug(f"  Company extraction error: {e}")
                return None

    # ── Profile Page Extraction ──────────────────────────────────────────

    async def _extract_profile_info(self, profile_url: str) -> Optional[dict]:
        """Visit a LinkedIn profile and extract contact + company info.

        Extracts:
          - Person's name, current title, current company
          - Company industry, location from profile section
        """
        await self._rate_limiter.acquire()

        async with await self._context.new_page() as li_page:
            try:
                await li_page.goto(
                    profile_url,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await asyncio.sleep(1.5)

                content = await li_page.content()

                if self._is_blocked_or_auth(content):
                    raise AuthErrorDetected(
                        self.source_name,
                        "LinkedIn security page on profile",
                    )

                soup = await self._parse_html(content)
                if soup is None:
                    return None

                # Extract name
                contact_name = None
                for selector in ["h1", ".text-heading-xlarge",
                                 ".pv-text-details__left-panel h1"]:
                    el = soup.select_one(selector)
                    if el:
                        contact_name = el.get_text(strip=True)
                        break

                # Extract current title
                title = None
                for selector in [
                    ".text-body-medium.break-words",
                    ".pv-text-details__left-panel div:nth-child(2)",
                ]:
                    el = soup.select_one(selector)
                    if el:
                        title = el.get_text(strip=True)
                        break

                # Extract current company
                company_name = None
                for selector in [
                    "a[href*='/company/'] span",
                    ".pv-entity__company-name",
                    "[data-test-id='primary-company-name']",
                ]:
                    el = soup.select_one(selector)
                    if el:
                        company_name = el.get_text(strip=True)
                        break

                # If no company found, try the experience section
                if not company_name:
                    exp_el = soup.select_one("#experience ~ .pvs-list__container a[href*='/company/'] span")
                    if exp_el:
                        company_name = exp_el.get_text(strip=True)

                if not company_name:
                    return None

                # Dedup by company
                name_key = company_name.lower()
                if name_key in self._seen_companies:
                    return None
                self._seen_companies.add(name_key)

                # Check decision-maker status
                is_decision_maker = title and any(
                    dm_title in title.lower()
                    for dm_title in DECISION_MAKER_TITLES
                )

                # Extract location
                country = None
                for selector in [
                    ".pv-text-details__left-panel .text-body-small",
                    ".text-body-small.inline",
                ]:
                    el = soup.select_one(selector)
                    if el:
                        location = el.get_text(strip=True)
                        country = self._extract_country(location)
                        if country:
                            break

                source_detail = {
                    "platform": "linkedin",
                    "profile_url": profile_url,
                    "contact_name": contact_name,
                    "title": title,
                    "is_decision_maker": is_decision_maker,
                }

                return {
                    "company_name": company_name,
                    "contact_name": contact_name,
                    "email": None,
                    "phone": None,
                    "whatsapp": None,
                    "website": None,
                    "industry": "fasteners",
                    "country": country,
                    "source_url": profile_url,
                    "source_detail": json.dumps(source_detail, ensure_ascii=False),
                    "notes": f"Title: {title}" if title else None,
                }

            except AuthErrorDetected:
                raise
            except Exception as e:
                logger.debug(f"  Profile extraction error: {e}")
                return None

    # ── Browser management ──────────────────────────────────────────────

    async def _ensure_browser(self):
        """Launch Playwright with persistent profile (login persists)."""
        if self._setup_done:
            return

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Launching LinkedIn browser (headless={self.headless})")
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            user_agent=config.SCRAPE_USER_AGENT,
            locale="en-US",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",       # needed for iframe-heavy LinkedIn
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            # Extra: disable automation detection
            bypass_csp=True,
        )
        self._setup_done = True

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _parse_html(self, content: str):
        """Parse HTML with BeautifulSoup."""
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(content, "lxml")
        except Exception:
            return None

    def _is_blocked_or_auth(self, html: str) -> bool:
        """Detect LinkedIn login or security challenge pages."""
        html_lower = html.lower()
        indicators_found = sum(
            1 for kw in LI_AUTH_INDICATORS if kw in html_lower
        )
        # High signal: page title mentions login or security
        has_login_title = any(
            title in html_lower
            for title in ["<title>linkedin login", "<title>sign in",
                          "<title>security verification"]
        )
        return indicators_found >= 3 or has_login_title

    @staticmethod
    def _is_fastener_relevant(text: str) -> bool:
        """Check if a company is relevant to the fastener industry."""
        keywords = [
            "fastener", "screw", "bolt", "nut", "washer", "rivet",
            "五金", "紧固件", "螺丝", "螺母", "螺栓",
            "industrial supply", "hardware", "metal parts",
            "machining", "stamping", "forging", "cnc",
            "threaded", "cold heading", "hot forging",
        ]
        return any(kw in text for kw in keywords)

    @staticmethod
    def _extract_country(location_text: str) -> Optional[str]:
        """Extract country name from a LinkedIn location string."""
        if not location_text:
            return None

        countries = [
            "United States", "USA", "Germany", "United Kingdom", "UK",
            "France", "Italy", "Spain", "Netherlands", "Poland",
            "Canada", "Australia", "Japan", "South Korea", "Brazil",
            "Mexico", "Turkey", "India", "Vietnam", "Thailand",
            "Indonesia", "Malaysia", "UAE", "Saudi Arabia", "South Africa",
            "Russia", "China", "Taiwan", "Singapore", "Sweden",
            "Switzerland", "Belgium", "Austria", "Portugal",
        ]

        text_lower = location_text.lower()
        for country in countries:
            if country.lower() in text_lower:
                # Map abbreviations
                if country == "USA":
                    return "United States"
                if country == "UK":
                    return "United Kingdom"
                return country

        return None

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        """Truncate text to max_len characters at word boundary."""
        if not text or len(text) <= max_len:
            return text
        truncated = text[:max_len]
        last_space = truncated.rfind(" ")
        return (truncated[:last_space] if last_space > max_len // 2 else truncated) + "..."

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def close(self):
        """Release Playwright resources."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        await super().close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
