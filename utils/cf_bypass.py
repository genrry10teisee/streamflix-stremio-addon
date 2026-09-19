"""
Cloudflare bypass module using Playwright (headless Chromium).

Cloudflare's "Just a moment..." JS challenge requires a real browser to
execute the challenge JS and obtain the cf_clearance cookie. We use
Playwright with a non-headless launch (visible) when possible, or with
stealth tweaks in headless mode.

Strategy:
  1. Launch Chromium (headless=new mode).
  2. Set a realistic User-Agent.
  3. Navigate to the URL. Cloudflare runs the challenge.
  4. Wait until the page reaches the actual content (no "Just a moment").
  5. Extract all cookies + HTML.
  6. Cache cookies per domain for ~30 minutes (cf_clearance usually lasts
     longer but we play it safe).

Usage:
    from utils.cf_bypass import fetch_with_clearance
    response = fetch_with_clearance("https://sololatino.net/peliculas")
    if response:
        html = response.text
"""

import os
import time
import json
import logging
import threading
from typing import Optional
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

# Cache: domain -> (expiry_ts, cookies_dict, user_agent)
_CLEARANCE_CACHE: dict[str, tuple[float, dict, str]] = {}
_CACHE_TTL = 30 * 60  # 30 minutes
_LOCK = threading.Lock()

# Optional: env var to disable bypass and always return None
DISABLED = os.environ.get("CF_BYPASS_DISABLED", "0") == "1"

# Optional: FlareSolverr URL (preferred if available, faster)
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "").rstrip("/")


def _is_cf_challenge(text: str) -> bool:
    """Detect Cloudflare challenge page."""
    indicators = [
        "Just a moment...",
        "cf_chl_opt",
        "challenge-platform",
        "Attention Required! | Cloudflare",
        "Sorry, you have been blocked",
    ]
    return any(ind in text for ind in indicators)


def _try_flaresolverr(url: str, max_timeout: int = 60) -> Optional[dict]:
    """Try FlareSolverr first (preferred, faster than Playwright)."""
    if not FLARESOLVERR_URL:
        return None
    try:
        r = requests.post(
            f"{FLARESOLVERR_URL}/v1",
            headers={"Content-Type": "application/json"},
            json={
                "cmd": "request.get",
                "url": url,
                "maxTimeout": max_timeout * 1000,
            },
            timeout=max_timeout + 10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("status") != "ok":
            log.warning(f"FlareSolverr error: {data.get('message', '?')}")
            return None
        sol = data.get("solution", {})
        cookies = {c["name"]: c["value"] for c in sol.get("cookies", [])}
        return {
            "html": sol.get("response", ""),
            "cookies": cookies,
            "user_agent": sol.get("userAgent", ""),
            "url": sol.get("url", url),
        }
    except Exception as e:
        log.warning(f"FlareSolverr failed: {e}")
        return None


def _try_playwright(url: str, timeout_ms: int = 30000) -> Optional[dict]:
    """
    Use Playwright to solve Cloudflare's JS challenge.

    This launches a real Chromium browser, navigates to the URL, waits for
    the challenge to resolve, then extracts cookies and HTML.
    """
    if DISABLED:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed; CF bypass unavailable")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1920,1080",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="es-ES",
            )
            # Remove webdriver flag
            context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['es-ES', 'es', 'en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                window.chrome = { runtime: {} };
                """
            )
            page = context.new_page()
            log.info(f"Playwright: navigating to {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # Wait for CF challenge to resolve. The "Just a moment..." page
            # reloads/redirects once the challenge is solved.
            deadline = time.time() + (timeout_ms / 1000)
            solved = False
            while time.time() < deadline:
                title = page.title()
                content = page.content()
                if not _is_cf_challenge(content) and "Just a moment" not in title:
                    solved = True
                    break
                time.sleep(1.0)

            if not solved:
                log.warning(f"Playwright: CF challenge not solved in {timeout_ms}ms")
                browser.close()
                return None

            # Wait a bit for any final redirects
            time.sleep(2.0)
            html = page.content()
            final_url = page.url
            cookies_list = context.cookies()
            cookies = {c["name"]: c["value"] for c in cookies_list}
            user_agent = page.evaluate("() => navigator.userAgent")
            browser.close()

            return {
                "html": html,
                "cookies": cookies,
                "user_agent": user_agent,
                "url": final_url,
            }
    except Exception as e:
        log.warning(f"Playwright bypass failed: {e}")
        return None


def _get_clearance(domain: str) -> Optional[tuple[dict, str]]:
    """
    Get cached cookies + UA for a domain, or solve CF challenge to get them.
    Returns (cookies_dict, user_agent) or None.
    """
    with _LOCK:
        cached = _CLEARANCE_CACHE.get(domain)
        if cached:
            expiry, cookies, ua = cached
            if time.time() < expiry and cookies.get("cf_clearance"):
                log.debug(f"CF clearance cache hit for {domain}")
                return cookies, ua

    # Cache miss or expired: solve challenge
    url = f"https://{domain}"
    log.info(f"CF clearance cache miss for {domain}; solving...")

    # Try FlareSolverr first
    sol = _try_flaresolverr(url)
    if not sol:
        # Fall back to Playwright
        sol = _try_playwright(url)

    if not sol:
        return None

    cookies = sol["cookies"]
    ua = sol["user_agent"]

    if not cookies.get("cf_clearance"):
        log.warning(f"No cf_clearance cookie obtained for {domain}")
        # Still cache it; maybe the site doesn't actually require it

    with _LOCK:
        _CLEARANCE_CACHE[domain] = (time.time() + _CACHE_TTL, cookies, ua)

    return cookies, ua


def fetch_with_clearance(
    url: str,
    referer: Optional[str] = None,
    method: str = "GET",
    json_body: Optional[dict] = None,
    timeout: int = 15,
) -> Optional[requests.Response]:
    """
    Fetch a URL with Cloudflare clearance cookies.

    If the domain has CF protection, solves the challenge first then makes
    the request with the cf_clearance cookie + matching User-Agent.

    Returns a requests.Response or None on failure.
    """
    domain = urlparse(url).netloc

    # First try a plain request (works for non-CF pages, fast)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer

    try:
        if method == "POST" and json_body is not None:
            r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
        else:
            r = requests.get(url, headers=headers, timeout=timeout)

        # If not a CF challenge, return as-is
        if not _is_cf_challenge(r.text) and r.status_code == 200:
            return r
    except Exception as e:
        log.debug(f"Plain request to {url} failed: {e}")

    # CF challenge detected (or other issue): try with clearance
    log.info(f"CF challenge detected for {url}; obtaining clearance...")
    clearance = _get_clearance(domain)
    if not clearance:
        log.warning(f"Could not obtain CF clearance for {domain}")
        return None

    cookies, ua = clearance
    headers["User-Agent"] = ua
    headers["Referer"] = referer or f"https://{domain}/"

    try:
        if method == "POST" and json_body is not None:
            r = requests.post(
                url, json=json_body, headers=headers, cookies=cookies, timeout=timeout
            )
        else:
            r = requests.get(url, headers=headers, cookies=cookies, timeout=timeout)

        if _is_cf_challenge(r.text):
            # Clearance expired or invalid; invalidate cache and retry once
            with _LOCK:
                _CLEARANCE_CACHE.pop(domain, None)
            log.warning(f"CF clearance invalid for {domain}; invalidated cache")
            return None
        return r
    except Exception as e:
        log.warning(f"Request with clearance failed: {e}")
        return None


def is_enabled() -> bool:
    """Return True if CF bypass is available."""
    if DISABLED:
        return False
    if FLARESOLVERR_URL:
        return True
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False
