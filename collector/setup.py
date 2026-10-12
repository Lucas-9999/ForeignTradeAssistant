"""
Browser setup script — first-time login for collectors requiring authentication.

Supports:
  - xunkexing:  Open visible browser, login → save profile → extract API token.
  - facebook:   Manual login → auto-detect → auto-collect → auto-close.
                After login is detected, collection starts immediately and
                results are saved to CRM. Browser closes automatically.
  - linkedin:   Open visible browser, login to LinkedIn → save persistent session.

Usage:
    python -m collector.setup xunkexing
    python -m collector.setup facebook
    python -m collector.setup linkedin
"""

from __future__ import annotations

import asyncio
import sys
import logging
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("collector.setup")


async def setup_xunkexing():
    """Open browser for manual XunKeXing login, save profile for token refresh."""
    from collector.browser import BrowserManager
    import config

    profile_dir = Path(config.DATA_DIR) / "xkx_profile"
    url = "https://www.xunkexing.cn"

    print(f"""
{'='*60}
  XunKeXing — First-Time Login Setup
{'='*60}

  A visible browser will open to: {url}
  (Redirects to sys.shijiyunhai.cn/xkxpc/ after login)

  1. Log in with your phone & password
  2. After login, you should see the main dashboard
  3. Return here and press Enter

  Profile saved to: {profile_dir}
  The collector auto-extracts API tokens from this profile.

  No need to run this again unless tokens expire (~30 days).
{'='*60}
""")

    input("Press Enter to open browser...")

    async with BrowserManager(
        headless=False,
        profile_dir=profile_dir,
    ) as bm:
        page = await bm.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)

        # Try to extract token to verify login
        token = await page.evaluate(
            "() => {"
            "  const ui = localStorage.getItem('userInfo');"
            "  if (ui) {"
            "    try { const u = JSON.parse(ui); return u.user_nicename || 'OK'; } catch(e) {}"
            "  }"
            "  return null;"
            "}"
        )

        if token:
            print(f"\nLogin detected! User: {token}")
            print("Token will be cached for API collection.")
        else:
            print("\n>>> Please log in manually in the browser window.")
            print(">>> After login, press Enter here.\n")
            input("Press Enter when logged in...")

            # Re-check
            token = await page.evaluate(
                "() => {"
                "  const ui = localStorage.getItem('userInfo');"
                "  if (ui) { try { return JSON.parse(ui).user_nicename; } catch(e) {} }"
                "  return null;"
                "}"
            )
            if token:
                print(f"Login verified! User: {token}")
            else:
                print("WARNING: Could not verify login. Try again.")

        await page.screenshot(path=str(profile_dir / "login_success.png"))

    print(f"\nSetup complete! Token profile saved to: {profile_dir}")
    print("Test collection:")
    print("  python -m collector run --source=xunkexing")


# ═══════════════════════════════════════════════════════════════════════════
# Facebook Setup (Auto-Detect Login)
# ═══════════════════════════════════════════════════════════════════════════

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


def _clear_corrupted_session(profile_dir: Path):
    """Remove corrupted cookies from a previous EPIPE crash.

    Chromium writes cookie rows with empty values when the browser
    crashes mid-write. These empty cookies block login detection.
    """
    cookies_db = profile_dir / "Default" / "Network" / "Cookies"
    if not cookies_db.exists():
        return

    import sqlite3
    try:
        conn = sqlite3.connect(str(cookies_db))
        bad = conn.execute(
            "SELECT count(*) FROM cookies "
            "WHERE name IN ('c_user','xs') AND length(value) = 0"
        ).fetchone()[0]
        conn.close()

        if bad > 0:
            print("Clearing corrupted session from previous crash...")
            cookies_db.unlink()
            for suffix in ['-journal', '-wal', '.db-journal', '.db-wal']:
                alt = cookies_db.with_name(cookies_db.name + suffix)
                if alt.exists():
                    alt.unlink()
    except Exception:
        pass


def _write_cookies_to_db(profile_dir: Path, cookies: list[dict]):
    """Persist cookies captured from Playwright API into Chromium's SQLite DB.

    Chromium persistent context on Windows sometimes writes empty cookie
    rows to SQLite even when valid cookies exist in browser memory.
    This function manually INSERTs or UPDATEs the cookies table with
    the real values from Playwright's context.cookies() API.

    Cookie schema (Chromium):
      creation_utc, host_key, name, value, path, expires_utc, is_secure,
      is_httponly, last_access_utc, has_expires, is_persistent, priority,
      encrypted_value, samesite, source_scheme, source_port, is_same_party
    """
    import sqlite3
    import time as _time

    cookies_db = profile_dir / "Default" / "Network" / "Cookies"
    if not cookies_db.exists():
        print("  [WARN] No Cookies DB to write to")
        return

    now_us = int(_time.time() * 1_000_000)  # microseconds since epoch

    conn = sqlite3.connect(str(cookies_db))
    try:
        written = 0
        for c in cookies:
            name = c.get('name', '')
            value = c.get('value', '')
            if not name or not value:
                continue
            domain = c.get('domain', '')
            path = c.get('path', '/')
            secure = 1 if c.get('secure', False) else 0
            httponly = 1 if c.get('httpOnly', False) else 0
            expires = c.get('expires', -1)
            if expires and expires > 0:
                expires_us = int(expires * 1_000_000)
                has_expires = 1
            else:
                expires_us = 0
                has_expires = 0
            same_site = c.get('sameSite', '')
            if same_site and same_site.lower() == 'unspecified':
                same_site = -1
            elif same_site and same_site.lower() == 'lax':
                same_site = 1
            elif same_site and same_site.lower() == 'strict':
                same_site = 2
            elif same_site and same_site.lower() in ('none', 'no_restriction'):
                same_site = 0
            else:
                same_site = -1  # unspecified

            # Check if cookie already exists in DB
            existing = conn.execute(
                "SELECT rowid FROM cookies WHERE host_key=? AND name=?",
                (domain, name),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE cookies SET value=?, encrypted_value='',
                       expires_utc=?, is_secure=?,
                       is_httponly=?, last_access_utc=?, has_expires=?
                       WHERE host_key=? AND name=?""",
                    (value, expires_us, secure, httponly, now_us, has_expires,
                     domain, name),
                )
            else:
                conn.execute(
                    """INSERT INTO cookies (creation_utc, host_key, name, value,
                       path, expires_utc, is_secure, is_httponly, last_access_utc,
                       has_expires, is_persistent, priority, encrypted_value,
                       samesite, source_scheme, source_port, is_same_party)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, '',
                               ?, 2, -1, 0)""",
                    (now_us, domain, name, value, path, expires_us, secure,
                     httponly, now_us, has_expires, same_site),
                )
            written += 1

        conn.commit()
        print(f"  [OK] Wrote {written} cookies to SQLite DB")
    except Exception as e:
        print(f"  [WARN] Cookie DB write failed: {e}")
    finally:
        conn.close()


def _verify_fb_session(profile_dir: Path) -> bool | None:
    """Verify Facebook session integrity by checking critical auth cookies.

    Returns:
        True  = session valid (c_user + xs have non-empty values)
        False = session invalid (missing or empty cookies)
        None  = could not verify (no cookies DB)
    """
    import sqlite3

    cookies_db = profile_dir / "Default" / "Network" / "Cookies"
    if not cookies_db.exists():
        print("\n  WARNING: No Cookies database found!")
        print("  Session was NOT saved — please re-run setup.")
        return None

    try:
        conn = sqlite3.connect(f"file:{cookies_db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT name, length(value) FROM cookies "
            "WHERE (host_key LIKE '%facebook%' OR host_key LIKE '%fb%')"
        ).fetchall()
        conn.close()

        cookie_info = {r[0]: r[1] for r in rows}
        critical = {"c_user", "xs"}
        found = critical & set(cookie_info)
        missing = critical - found

        if missing:
            print(f"\n  WARNING: Missing critical auth cookies: {missing}")
            print(f"  Found ({len(cookie_info)}): {sorted(cookie_info)}")
            print("  Re-run: python -m collector.setup facebook")
            return False

        empty_critical = [c for c in found if cookie_info.get(c, 0) == 0]
        if empty_critical:
            print(f"\n  WARNING: Auth cookies present but EMPTY: {empty_critical}")
            print("  This happens when the browser crashes during login (EPIPE).")
            print("  Re-run: python -m collector.setup facebook")
            return False

        print(f"  Session verified — {len(cookie_info)} FB cookies with valid values")
        print(f"  Critical auth: c_user + xs present and non-empty")
        return True

    except Exception as e:
        print(f"\n  Could not verify session: {e}")
        print("  Test with: python -m collector run --source=facebook --dry-run")
        return None


async def setup_facebook():
    """Open browser for manual Facebook login -> auto-collect -> auto-close.

    Flow:
      1. Opens visible desktop browser to www.facebook.com
      2. You log in manually (with 2FA if needed)
      3. Script auto-detects login completion (URL + content signals)
      4. Automatically starts lead collection (visible browser)
      5. Collection results saved to CRM -- summary printed
      6. Browser auto-closes when done

    Uses desktop site for login (more reliable, cookies scoped to .facebook.com).
    Collection phase uses mbasic + mobile UA for parsing.
    """
    from playwright.async_api import async_playwright
    import config
    import re
    import time

    profile_dir = Path(config.DATA_DIR) / "fb_profile"
    url = "https://www.facebook.com"

    # Clean up corrupted session from previous EPIPE crash
    _clear_corrupted_session(profile_dir)

    print(f"""
{'='*60}
  Facebook -- Manual Login -> Auto-Collect
{'='*60}

  A visible browser will open to: {url}

  1. Log in with your Facebook account
  2. Complete any 2FA / security check if prompted
  3. Login is auto-detected -> collection starts automatically
  4. Browser auto-closes when collection is complete

  Profile: {profile_dir}
{'='*60}
""")

    playwright = await async_playwright().start()
    profile_dir.mkdir(parents=True, exist_ok=True)

    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-webrtc", "--disable-geolocation",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-hang-monitor", "--disable-infobars",
        "--disable-breakpad", "--disable-sync",
        "--no-default-browser-check", "--disable-component-update",
        "--lang=en-US",
    ]

    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=False,
        viewport={"width": 1280, "height": 900},
        user_agent=config.SCRAPE_USER_AGENT,
        locale="en-US",
        timezone_id="America/Chicago",
        args=browser_args,
        bypass_csp=True,
        ignore_https_errors=True,
    )

    page = context.pages[0] if context.pages else await context.new_page()

    # Navigate & inject stealth
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(1)
    try:
        await page.evaluate(
            "() => {"
            "  Object.defineProperty(navigator, 'webdriver', { get: () => false });"
            "  Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });"
            "  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'zh-CN'] });"
            "  window.chrome = { runtime: {} };"
            "}"
        )
    except Exception:
        pass
    await asyncio.sleep(2)

    # -- Auto-detect login (poll every 3s, 5 min timeout) -------------
    LOGIN_TIMEOUT = 300
    POLL_INTERVAL = 3

    print("Waiting for login... (auto-detect, 5 min timeout)")
    print("Complete login in the browser window -- collection starts automatically.\n")

    logged_in = False
    start = time.time()
    last_url = ""
    seen_save_device = False  # FB shows /login/save-device/ after successful login

    while time.time() - start < LOGIN_TIMEOUT:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            current_url = page.url
            try:
                content = await page.content()
            except Exception:
                # Page is navigating — skip this poll cycle
                continue
            url_lower = current_url.lower()
            content_lower = content.lower()

            # -- URL-based detection (strongest signal) -----------------
            # After login, FB redirects through: login -> save-device -> homepage
            if "/login/save-device/" in url_lower or "save-device" in url_lower:
                seen_save_device = True
                elapsed = int(time.time() - start)
                print(f"  [{elapsed}s] Save-device page -- login successful!")
                continue

            # If we've seen save-device and now we're on the homepage, we're logged in
            if seen_save_device and (
                current_url.rstrip("/") in (
                    "https://www.facebook.com",
                    "https://www.facebook.com/",
                )
                or url_lower in ("https://www.facebook.com/", "https://www.facebook.com")
            ):
                logged_in = True
                break

            # Also detect: redirect from login page to feed/home
            if seen_save_device and (
                "/?sk=h_chr" in url_lower
                or "facebook.com/?deoia" in url_lower
                or "facebook.com/?" in url_lower
            ):
                logged_in = True
                break

            # -- Content-based detection (fallback) ---------------------
            # Login form indicators (desktop FB)
            login_form_hits = sum(1 for ind in [
                'id="email"', 'id="pass"', 'name="login"',
                'id="loginbutton"', 'id="login_form"',
            ] if ind in content_lower)
            is_login = (
                login_form_hits >= 2
                or '<title>log in' in content_lower
                or '<title>facebook – log in' in content_lower
                or 'log in to facebook' in content_lower
            )

            # Logged-in signals (desktop FB)
            logged_in_hits = sum(1 for kw in [
                'aria-label="home"', 'aria-label="your profile"',
                'aria-label="notifications"', 'aria-label="messenger"',
                'aria-label="create"', 'aria-label="menu"',
                'find friends', 'welcome to facebook',
                'status",null',  # FB reactive state in logged-in pages
            ] if kw in content_lower)

            # Strong signal: FB's logged-in page title is just "Facebook" or "(N+) Facebook"
            has_notification_count = bool(re.search(
                r'<title>\(\d+\+?\)\s*facebook</title>', content_lower
            ))

            if not is_login and (logged_in_hits >= 3 or has_notification_count):
                logged_in = True
                break

            # Detect checkpoint / security challenge (only log once per URL)
            is_security_page = any(
                pat in url_lower for pat in [
                    "/checkpoint/", "/recover/", "/two_factor/",
                    "/two_step_verification/",
                ]
            )
            if is_security_page and current_url != last_url:
                elapsed = int(time.time() - start)
                print(f"  [{elapsed}s] Security/2FA page -- complete it in browser...")

            if current_url != last_url:
                elapsed = int(time.time() - start)
                url_short = current_url[:100]
                if not is_security_page:
                    print(f"  [{elapsed}s] {url_short}")
                last_url = current_url

        except Exception as e:
            elapsed = int(time.time() - start)
            print(f"\n  [{elapsed}s] Browser connection interrupted: {e}")
            print("  Checking if session was saved anyway...")
            break

    if logged_in:
        elapsed = int(time.time() - start)
        print(f"\n  [OK] Login detected! ({elapsed}s)")
        try:
            name_el = page.locator("a[aria-label*='profile'] span, "
                                   "[data-pagelet='LeftRail'] a[href*='/me']")
            if await name_el.count() > 0:
                name = await name_el.first.text_content()
                if name and name.strip():
                    print(f"  User: {name.strip()}")
        except Exception:
            pass

        # -- Read cookies via Playwright API (in-memory) -------------------
        print("\n  Checking browser cookies...")
        try:
            all_cookies = await context.cookies()
            fb_cookies = [c for c in all_cookies
                          if 'facebook' in c.get('domain', '')]
            critical = {}
            for c in fb_cookies:
                if c['name'] in ('c_user', 'xs'):
                    critical[c['name']] = c['value']
            c_user_val = critical.get('c_user', '')
            xs_val = critical.get('xs', '')
            print(f"  FB cookies in browser: {len(fb_cookies)}")
            print(f"  c_user = {'***' + c_user_val[-4:] if c_user_val else '(empty)'}")
            print(f"  xs     = {'***' + xs_val[-4:] if xs_val else '(empty)'}")

            if c_user_val and xs_val:
                print("  [OK] Auth cookies present in browser memory")
            else:
                print("  [WARN] Auth cookies empty -- login may be incomplete")
        except Exception as e:
            print(f"  Cookie check error: {e}")

        # -- Explicitly save storage state to JSON (bypass SQLite) ---------
        storage_path = profile_dir / "storage_state.json"
        print(f"\n  Saving storage state to {storage_path}...")
        try:
            await context.storage_state(path=str(storage_path))
            print("  [OK] Storage state saved (cookies + localStorage)")
        except Exception as e:
            print(f"  [FAIL] Could not save storage state: {e}")
    else:
        elapsed = int(time.time() - start)
        if elapsed >= LOGIN_TIMEOUT:
            print(f"\n  Timeout ({LOGIN_TIMEOUT}s) -- saving whatever session exists.")
        print("  Press Enter to save current state and try collection anyway...")
        try:
            input()
        except (EOFError, OSError):
            pass

    # -- Save screenshot -------------------------------------------------
    try:
        await page.screenshot(path=str(profile_dir / "login_success.png"))
    except Exception:
        pass

    # -- Capture cookies from browser memory BEFORE closing ------------
    # Chromium persistent context on Windows often writes empty cookie
    # rows to SQLite. Workaround: read cookies via Playwright API and
    # manually persist them to the SQLite DB after browser shutdown.
    cookies_for_db: list[dict] = []
    if logged_in:
        try:
            all_cookies = await context.cookies()
            cookies_for_db = [
                c for c in all_cookies
                if 'facebook' in c.get('domain', '')
            ]
            print(f"\n  Captured {len(cookies_for_db)} FB cookies from browser memory")
            for c in cookies_for_db:
                if c['name'] in ('c_user', 'xs'):
                    val_preview = c['value'][:12] if c['value'] else '(empty)'
                    print(f"    {c['name']} = {val_preview}... ok")
        except Exception as e:
            print(f"  [WARN] Could not capture cookies: {e}")

    # -- Close setup browser -------------------------------------------
    print("\nSaving session...")
    try:
        await context.close()
    except Exception:
        pass
    try:
        await playwright.stop()
    except Exception:
        pass

    await asyncio.sleep(1.0)

    # -- Manually persist cookies to SQLite (bypass Chromium bug) ------
    if cookies_for_db:
        _write_cookies_to_db(profile_dir, cookies_for_db)

    # -- Verify session --------------------------------------------------
    print()
    ok = _verify_fb_session(profile_dir)
    if ok is False:
        print("\n[FAIL] Session NOT saved correctly. Please re-run setup.")
        print("  python -m collector.setup facebook")
        return
    elif ok is None:
        print("\n[WARN] Session may not be valid. Attempting collection anyway...")
    else:
        print("\n[OK] Session verified -- starting collection...\n")

    # -- Auto-collect ----------------------------------------------------
    await _collect_after_login(profile_dir)


# ═══════════════════════════════════════════════════════════════════════════
# Post-Login Auto-Collection
# ═══════════════════════════════════════════════════════════════════════════


async def _collect_after_login(profile_dir: Path):
    """Run Facebook collection immediately after login, then close browser.

    Uses the freshly-saved session in profile_dir. The collector opens
    its own browser (persistent context from the same profile), so the
    setup browser must be fully closed before calling this.
    """
    import config
    from collector.sources.facebook import FacebookCollector
    from collector.pipeline import ingest_leads
    from crm.db import get_connection
    from crm.manager import LeadManager

    max_leads = getattr(config, 'FB_COLLECTION_MAX_LEADS', 80)

    print(f"{'='*60}")
    print(f"  Starting Facebook lead collection (max {max_leads} leads)...")
    print(f"{'='*60}\n")

    collector = FacebookCollector(
        headless=False,          # visible = less bot detection from FB
        max_posts=max_leads,
        profile_dir=profile_dir,
    )

    leads_found = 0
    leads_new = 0
    dupes = 0
    errors = 0

    try:
        raw_leads = await collector.collect()
        leads_found = len(raw_leads)

        if not raw_leads:
            print("\n[WARN] No leads collected. Possible causes:")
            print("  - Session still invalid (try re-running setup)")
            print("  - Facebook blocked the request (try again later)")
            print("  - All search queries returned 0 parseable results")
            return

        # ── Ingest into CRM ──────────────────────────────────────────
        db_path = config.DB_PATH
        if not db_path.exists():
            from crm.db import build_database
            build_database(db_path)

        conn = get_connection(db_path)
        try:
            lead_mgr = LeadManager(conn)
            result = ingest_leads(raw_leads, "facebook", lead_mgr)
            leads_new = result.get("created", 0)
            dupes = result.get("duplicates_skipped", 0)
            errors = result.get("errors", 0)
        finally:
            conn.close()

        # ── Summary ──────────────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"  Collection Complete!")
        print(f"{'='*60}")
        print(f"  Raw leads found:    {leads_found}")
        print(f"  New leads saved:    {leads_new}")
        if leads_found > 0:
            print(f"  Duplicates skipped: {dupes}")
            if errors:
                print(f"  Errors:             {errors}")
        print(f"{'='*60}\n")

        # ── Log to scraping_tasks ────────────────────────────────────
        _log_fb_collection_task(leads_found, leads_new)

    except Exception as e:
        logger.exception("Auto-collection failed")
        print(f"\n[FAIL] Collection failed: {e}")
        print("  Session is saved — retry with:")
        print("    python -m collector run --source=facebook")
    finally:
        # ── Always close the browser ─────────────────────────────────
        print("Closing browser...")
        try:
            await collector.close()
        except Exception:
            pass
        print("Done.\n")


def _log_fb_collection_task(leads_found: int, leads_new: int):
    """Record the collection run in scraping_tasks for dashboard visibility."""
    import sqlite3
    import config
    from datetime import datetime, timezone

    db_path = config.DB_PATH
    if not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.execute(
            "INSERT INTO scraping_tasks (source, status, leads_found, leads_new, "
            "started_at, completed_at) VALUES ('facebook', 'completed', ?, ?, ?, ?)",
            (leads_found, leads_new, now, now),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# LinkedIn Setup
# ═══════════════════════════════════════════════════════════════════════════

async def setup_linkedin():
    """Open browser for manual LinkedIn login, save persistent session."""
    from playwright.async_api import async_playwright
    import config

    profile_dir = Path(config.DATA_DIR) / "linkedin_profile"
    url = "https://www.linkedin.com/login"

    print(f"""
{'='*60}
  LinkedIn — First-Time Login Setup
{'='*60}

  A visible browser will open to: {url}

  1. Log in with your LinkedIn account
  2. After login, verify you can see the feed
  3. Return here and press Enter

  Profile saved to: {profile_dir}

  IMPORTANT:
  - Use a dedicated LinkedIn account (not your personal one)
  - LinkedIn aggressively detects automation — keep volumes low
{'='*60}
""")

    input("Press Enter to open browser...")

    playwright = await async_playwright().start()
    profile_dir.mkdir(parents=True, exist_ok=True)

    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=False,
        viewport={"width": 1280, "height": 900},
        user_agent=config.SCRAPE_USER_AGENT,
        locale="en-US",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(2)

    print("\n>>> Please log in to LinkedIn in the browser window.")
    print(">>> Complete any 2FA / security checks if prompted.")
    print(">>> After you see the LinkedIn feed, press Enter here.\n")
    input("Press Enter when logged in...")

    # Verify login
    content = await page.content()
    login_indicators = ["sign in", "login", "password", "forgot password"]
    still_login = sum(1 for kw in login_indicators
                      if kw in content.lower())

    if still_login >= 3:
        print("\nWARNING: Login may not have been detected.")
    else:
        await page.goto("https://www.linkedin.com/feed/",
                        wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(1)
        feed_content = await page.content()
        if "feed" in feed_content.lower():
            print("\nLogin verified! Feed page accessible.")
        else:
            print("\nLogin session captured. Profile saved.")

    await page.screenshot(path=str(profile_dir / "login_success.png"))
    await context.close()
    await playwright.stop()

    print(f"\nSetup complete! LinkedIn profile saved to: {profile_dir}")
    print("Test collection (DRY RUN FIRST):")
    print("  python -m collector run --source=linkedin --dry-run")


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m collector.setup xunkexing")
        print("  python -m collector.setup facebook")
        print("  python -m collector.setup linkedin")
        sys.exit(1)

    source = sys.argv[1]
    if source == "xunkexing":
        asyncio.run(setup_xunkexing())
    elif source == "facebook":
        asyncio.run(setup_facebook())
    elif source == "linkedin":
        asyncio.run(setup_linkedin())
    else:
        print(f"Unknown source: {source}")
        print("Available: xunkexing, facebook, linkedin")
        sys.exit(1)


if __name__ == "__main__":
    main()
