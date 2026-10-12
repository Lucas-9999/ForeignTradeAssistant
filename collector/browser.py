"""
Playwright Browser Manager — singleton browser lifecycle for collectors.

Supports two modes:
  1. Chromium with cookie persistence (default)
  2. Persistent profile — uses a dedicated user data directory so login
     sessions survive between runs. First run should use headless=False
     to manually log in; subsequent runs work in headless mode.

Used by collectors that need JavaScript rendering (XunKeXing, Facebook, etc.).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

import config

logger = logging.getLogger(__name__)

DEFAULT_COOKIES_PATH = Path(config.DATA_DIR) / "browser_cookies.json"
DEFAULT_PROFILE_DIR = Path(config.DATA_DIR) / "browser_profile"


class BrowserManager:
    """Singleton manager for a Playwright browser instance.

    Usage:
        # Persistent profile (recommended for login-required sites):
        async with BrowserManager(profile_dir="data/xkx_profile", headless=False) as bm:
            page = await bm.new_page()
            # ... first run: log in manually. Subsequent runs: session persists.

        # Cookie-only mode (lighter, for sites without strict login):
        async with BrowserManager() as bm:
            page = await bm.new_page()
    """

    def __init__(
        self,
        headless: bool = True,
        cookies_path: Optional[Path] = None,
        profile_dir: Optional[Path] = None,
        viewport: dict | None = None,
    ):
        self.headless = headless
        self.cookies_path = cookies_path or DEFAULT_COOKIES_PATH
        self.profile_dir = profile_dir  # None = cookie mode, Path = persistent profile mode
        self.viewport = viewport or {"width": 1280, "height": 900}
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._is_persistent = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> BrowserContext:
        """Launch browser and return a context."""
        if self._context is not None:
            return self._context

        self._playwright = await async_playwright().start()

        if self.profile_dir:
            await self._start_persistent()
        else:
            await self._start_ephemeral()

        logger.info(
            f"Browser ready (headless={self.headless}, "
            f"mode={'persistent' if self._is_persistent else 'cookie'})"
        )
        return self._context

    async def _start_persistent(self):
        """Launch with a persistent user data directory.

        The profile directory stores cookies, localStorage, saved passwords, etc.
        Once logged in, the session survives browser restarts.
        """
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Launching with persistent profile: {self.profile_dir}")

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport=self.viewport,
            user_agent=config.SCRAPE_USER_AGENT,
            locale="zh-CN",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self._is_persistent = True

    async def _start_ephemeral(self):
        """Launch a fresh browser and restore cookies from file."""
        logger.info(f"Launching Chromium (headless={self.headless})...")
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(
            viewport=self.viewport,
            user_agent=config.SCRAPE_USER_AGENT,
            locale="zh-CN",
        )
        await self._load_cookies()
        self._is_persistent = False

    async def new_page(self) -> Page:
        """Create a new page in the browser context."""
        if self._context is None:
            await self.start()
        page = await self._context.new_page()
        return page

    async def shutdown(self) -> None:
        """Close browser and playwright."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        # Persistent context doesn't have a separate browser object
        if self._context:
            # For persistent context, closing the context closes the browser
            if self._is_persistent:
                await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser shut down")

    # ── Cookie persistence (ephemeral mode only) ───────────────────────────

    async def save_cookies(self) -> None:
        """Save browser context cookies to a JSON file.

        In persistent mode this is unnecessary — cookies survive in the profile.
        In ephemeral mode, saves cookies so they can be restored next run.
        """
        if self._is_persistent:
            return  # Persistent profile handles this automatically
        if self._context is None:
            return
        cookies = await self._context.cookies()
        self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cookies_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved {len(cookies)} cookies to {self.cookies_path}")

    async def _load_cookies(self) -> None:
        """Load cookies from file into the browser context."""
        if not self.cookies_path.exists():
            return
        try:
            with open(self.cookies_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if cookies:
                await self._context.add_cookies(cookies)
                logger.info(f"Loaded {len(cookies)} cookies from {self.cookies_path}")
        except Exception:
            logger.warning(f"Failed to load cookies from {self.cookies_path}")

    def clear_session(self) -> None:
        """Delete persisted cookies or profile (forces fresh login)."""
        if self.profile_dir and self.profile_dir.exists():
            import shutil
            shutil.rmtree(self.profile_dir)
            logger.info(f"Cleared profile: {self.profile_dir}")
        if self.cookies_path.exists():
            self.cookies_path.unlink()
            logger.info(f"Cleared cookies: {self.cookies_path}")

    # ── Async context manager ─────────────────────────────────────────────

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.shutdown()
