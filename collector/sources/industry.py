"""
Industry Site Collector — scrapes B2B directories and industry websites.

Supports configurable multi-site collection via data/industry_sites.json.
Each site defines CSS selectors for list pages and detail pages.

Pattern: two-phase collection
  1. List phase: crawl list/search pages → collect detail URLs
  2. Detail phase: visit each detail page → extract company info
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from collector.base import BaseCollector
import config

logger = logging.getLogger(__name__)

# Default path to site configurations
DEFAULT_SITES_CONFIG = Path(config.DATA_DIR) / "industry_sites.json"
# Per-site run state (consecutive 0-lead runs → auto-skip)
DEFAULT_SITES_STATE = Path(config.DATA_DIR) / "industry_site_state.json"
# Consecutive 0-lead runs before a site is auto-skipped (selector rot / anti-bot)
AUTO_SKIP_AFTER_ZERO_RUNS = 3
_PUBLIC_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.IGNORECASE,
)


class IndustryCollector(BaseCollector):
    """Multi-site industry directory collector.

    Loads site configurations from a JSON file and crawls each enabled site.
    Extracts company information using CSS selectors defined per-site.
    """

    source_name = "industry_site"

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        delay: float = config.SCRAPE_REQUEST_DELAY,
        sites_config: Optional[Path] = None,
        max_detail_pages: Optional[int] = None,
    ):
        super().__init__(client=client, delay=delay)
        self.sites_config = sites_config or DEFAULT_SITES_CONFIG
        self._global_max_details = max_detail_pages  # override per-site if set
        self._visited_urls: set[str] = set()
        # Lazy BrowserManager — only launched for sites with use_browser=true
        self._browser_mgr = None
        self._site_state = self._load_state()

    # ── Main entry point ─────────────────────────────────────────────────

    async def collect(self) -> list[dict]:
        """Run collection across all enabled sites. Returns combined lead list."""
        sites = self._load_config()
        if not sites:
            logger.warning("No enabled sites found in config")
            return []

        all_leads: list[dict] = []
        for site_cfg in sites:
            name = site_cfg["name"]
            if self._site_auto_skipped(name):
                logger.warning(
                    f"  Skipping {name} — auto-disabled after "
                    f"{AUTO_SKIP_AFTER_ZERO_RUNS} consecutive 0-lead runs "
                    f"(re-verify selectors, then clear data/industry_site_state.json)"
                )
                continue
            logger.info(f"Collecting from: {name}")
            try:
                leads = await self._collect_site(site_cfg)
                logger.info(f"  {name}: {len(leads)} leads found")
                all_leads.extend(leads)
                self._record_site_result(name, len(leads))
            except Exception:
                logger.exception(f"  {name}: FAILED")
                self._record_site_result(name, 0)

        if self._browser_mgr is not None:
            try:
                await self._browser_mgr.shutdown()
                self._browser_mgr = None
            except Exception:
                logger.exception("Failed to shut down industry browser")

        return all_leads

    async def close(self) -> None:
        """Release HTTP client and any launched browser."""
        await super().close()
        if self._browser_mgr is not None:
            try:
                await self._browser_mgr.shutdown()
            except Exception:
                logger.exception("Failed to shut down industry browser")
            finally:
                self._browser_mgr = None

    # ── Config loading ────────────────────────────────────────────────────

    def _load_config(self) -> list[dict]:
        """Load and return enabled site configurations."""
        if not self.sites_config.exists():
            logger.warning(f"Sites config not found: {self.sites_config}")
            return []
        with open(self.sites_config, "r", encoding="utf-8") as f:
            data = json.load(f)
        sites = data.get("sites", [])
        enabled = [s for s in sites if s.get("enabled", False)]
        logger.info(f"Loaded {len(enabled)} enabled / {len(sites)} total site configs")
        return enabled

    # ── Per-site run state (auto-disable on selector rot) ────────────────

    def _load_state(self) -> dict:
        if DEFAULT_SITES_STATE.exists():
            try:
                return json.loads(DEFAULT_SITES_STATE.read_text(encoding="utf-8"))
            except Exception:
                logger.warning(f"Could not read {DEFAULT_SITES_STATE.name}")
        return {}

    def _save_state(self) -> None:
        try:
            DEFAULT_SITES_STATE.parent.mkdir(parents=True, exist_ok=True)
            DEFAULT_SITES_STATE.write_text(
                json.dumps(self._site_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning(f"Could not write {DEFAULT_SITES_STATE.name}")

    def _site_auto_skipped(self, name: str) -> bool:
        st = self._site_state.get(name, {})
        return st.get("consecutive_zero", 0) >= AUTO_SKIP_AFTER_ZERO_RUNS

    def _record_site_result(self, name: str, found: int) -> None:
        st = self._site_state.setdefault(name, {"consecutive_zero": 0})
        if found == 0:
            st["consecutive_zero"] = st.get("consecutive_zero", 0) + 1
            if st["consecutive_zero"] >= AUTO_SKIP_AFTER_ZERO_RUNS:
                logger.warning(
                    f"  [{name}] auto-disabled: {st['consecutive_zero']} "
                    f"consecutive 0-lead runs — selectors likely rotted or "
                    f"anti-bot. Re-verify then clear state."
                )
        else:
            st["consecutive_zero"] = 0
        self._save_state()

    # ── Browser rendering (for anti-bot sites) ───────────────────────────

    async def _ensure_browser(self):
        """Lazily launch a headless Chromium for use_browser sites."""
        if self._browser_mgr is None:
            from collector.browser import BrowserManager
            self._browser_mgr = BrowserManager(headless=True)
            await self._browser_mgr.start()
        return self._browser_mgr

    async def _fetch_html(self, url: str, use_browser: bool) -> Optional[str]:
        """Fetch page HTML — httpx for static sites, Playwright for JS sites."""
        if not use_browser:
            resp = await self._get(url)
            return resp.text
        bm = await self._ensure_browser()
        async with await bm.new_page() as page:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Settle + scroll to trigger lazy-loaded listing content.
                await page.wait_for_timeout(2500)
                for _ in range(3):
                    await page.mouse.wheel(0, 1200)
                    await page.wait_for_timeout(400)
                return await page.content()
            except Exception as e:
                logger.warning(f"  Browser fetch failed for {url}: {e}")
                return None

    # ── Per-site collection ───────────────────────────────────────────────

    async def _collect_site(self, cfg: dict) -> list[dict]:
        """Collect leads from a single configured site."""
        max_details = self._global_max_details or cfg.get("max_detail_pages", 20)
        use_browser = bool(cfg.get("use_browser", False))
        leads: list[dict] = []

        for start_url in cfg.get("start_urls", []):
            if len(leads) >= max_details:
                break

            # Phase 1: Collect detail URLs from list pages
            detail_urls = await self._crawl_list_pages(
                cfg, start_url, max_details, use_browser
            )
            logger.debug(f"  Found {len(detail_urls)} detail URLs from {start_url}")

            # Phase 2: Visit each detail page and extract info
            for detail_url in detail_urls:
                if len(leads) >= max_details:
                    break
                if detail_url in self._visited_urls:
                    continue
                self._visited_urls.add(detail_url)

                lead = await self._extract_from_detail(cfg, detail_url, use_browser)
                if lead and lead.get("company_name"):
                    lead["source_url"] = detail_url
                    lead["source_detail"] = {
                        "site": cfg["name"],
                        "start_url": start_url,
                        "detail_url": detail_url,
                    }
                    leads.append(lead)

            if len(leads) >= max_details:
                break

        return leads

    # ── List page crawling ────────────────────────────────────────────────

    async def _crawl_list_pages(
        self, cfg: dict, start_url: str, max_details: int, use_browser: bool = False
    ) -> list[str]:
        """Crawl list/search pages to collect detail page URLs."""
        urls: list[str] = []
        max_pages = cfg.get("max_pages", 3)
        list_sel = cfg.get("list_selector", "")
        link_sel = cfg.get("detail_link_selector", "")
        next_sel = cfg.get("pagination_selector", "")

        current_url = start_url
        for page_num in range(1, max_pages + 1):
            if len(urls) >= max_details * 2:  # collect extra, filter later
                break

            try:
                html = await self._fetch_html(current_url, use_browser)
                if html is None:
                    logger.warning(f"  [{cfg['name']}] could not fetch {current_url}")
                    break
                soup = BeautifulSoup(html, "lxml")

                # Extract detail links from listing
                if list_sel:
                    cards = soup.select(list_sel)
                    for card in cards:
                        link = self._extract_link(card, link_sel)
                        if link:
                            full_url = urljoin(current_url, link)
                            urls.append(full_url)
                else:
                    # No list selector — extract all plausible links
                    for a_tag in soup.select("a[href]"):
                        href = a_tag.get("href", "")
                        if href and not href.startswith("#"):
                            urls.append(urljoin(current_url, href))

                # Find next page
                if next_sel and page_num < max_pages:
                    next_url = self._extract_link(soup, next_sel)
                    if next_url and next_url != current_url:
                        current_url = urljoin(current_url, next_url)
                    else:
                        break  # no more pages

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 403:
                    logger.warning(
                        f"  [{cfg['name']}] HTTP 403 Forbidden on {current_url} — "
                        f"site has anti-bot protection. This site likely requires "
                        f"Playwright browser mode. Consider disabling it in "
                        f"industry_sites.json"
                    )
                elif status == 404:
                    logger.warning(
                        f"  [{cfg['name']}] HTTP 404 Not Found on {current_url} — "
                        f"page no longer exists. Update the URL or disable this site "
                        f"in industry_sites.json"
                    )
                else:
                    logger.warning(f"  [{cfg['name']}] HTTP {status} on {current_url}")
                break
            except Exception:
                logger.exception(f"  Error crawling {current_url}")
                break

        # De-duplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        return unique[: max_details * 2]

    # ── Detail page extraction ─────────────────────────────────────────────

    async def _extract_from_detail(
        self, cfg: dict, url: str, use_browser: bool = False
    ) -> Optional[dict]:
        """Extract company information from a detail page."""
        try:
            html = await self._fetch_html(url, use_browser)
            if html is None:
                return None
            soup = BeautifulSoup(html, "lxml")
            return self._parse_fields(cfg, soup, url)
        except httpx.HTTPStatusError as e:
            logger.debug(f"  HTTP {e.response.status_code} on detail page {url}")
            return None
        except Exception:
            logger.debug(f"  Error extracting from {url}")
            return None

    def _parse_fields(self, cfg: dict, soup: BeautifulSoup, url: str) -> dict:
        """Extract fields from a BeautifulSoup object using configured selectors."""
        fields = cfg.get("fields", {})
        result: dict = {}

        for field_name, selector in fields.items():
            if field_name.startswith("_"):  # skip meta fields like _detail_page_only
                continue
            value = self._extract_text(soup, selector)
            if value:
                result[field_name] = value

        # Try to extract email from mailto links on the page
        if not result.get("email"):
            mailto = soup.select_one("a[href^=mailto]")
            if mailto:
                email = mailto.get("href", "").replace("mailto:", "").strip()
                if email and "@" in email:
                    result["email"] = email

        # Some public company pages print the business contact address as text
        # instead of a mailto link.  Keep this bounded to the already fetched
        # public page; no guessed addresses or third-party enrichment.
        if not result.get("email"):
            match = _PUBLIC_EMAIL_RE.search(soup.get_text(" ", strip=True))
            if match:
                result["email"] = match.group(0)

        # Try to extract website from external links
        if not result.get("website"):
            # Look for "website" text near a link
            for a_tag in soup.select("a[href]"):
                href = a_tag.get("href", "")
                text = a_tag.get_text().strip().lower()
                if text in ("website", "visit website", "web", "www", "homepage"):
                    result["website"] = href
                    break

        return result

    # ── Selector helpers ───────────────────────────────────────────────────

    def _extract_link(self, element: Tag | BeautifulSoup, selector: str) -> Optional[str]:
        """Extract the first href from a CSS selector match."""
        if not selector:
            return None
        try:
            el = element.select_one(selector) if isinstance(element, BeautifulSoup) else element
            if el is None:
                return None

            # Try multiple selectors (comma-separated)
            for sel in selector.split(","):
                sel = sel.strip()
                match = (element.select_one(sel)
                         if isinstance(element, BeautifulSoup)
                         else element.find_parent().select_one(sel) if hasattr(element, 'find_parent') else None)
                if match:
                    href = match.get("href")
                    if href:
                        return href.strip()

            # Fallback: just try the element itself
            if hasattr(el, "get"):
                href = el.get("href")
                if href:
                    return href.strip()

            return None
        except Exception:
            return None

    def _extract_text(self, soup: BeautifulSoup, selector: str) -> Optional[str]:
        """Extract clean text from the first matching CSS selector."""
        if not selector:
            return None

        # Support comma-separated fallback selectors (first match wins)
        for sel in selector.split(","):
            sel = sel.strip()
            try:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(separator=" ", strip=True)
                    # Clean excessive whitespace
                    text = " ".join(text.split())
                    if text:
                        return text
            except Exception:
                continue

        return None
