"""
Cloudflare bypass using nodriver (successor to undetected-chromedriver).

nodriver is purpose-built to defeat Cloudflare's bot detection. It uses
Chrome with CDP (no Selenium / no webdriver flag), making it nearly
indistinguishable from a real user.
"""

import asyncio
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

# Time to wait for CF challenge to resolve
CF_WAIT_SEC = int(os.environ.get("CF_WAIT_SEC", "30"))


async def _bypass(url: str) -> Optional[dict]:
    """Run nodriver to bypass CF and return html + cookies."""
    try:
        import nodriver as uc
    except ImportError:
        log.warning("nodriver not installed")
        return None

    browser = None
    try:
        browser = await uc.start(
            headless=True,
            browser_args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--lang=es-ES",
            ],
        )

        page = await browser.get(url)
        # Wait for CF challenge to resolve
        deadline = time.time() + CF_WAIT_SEC
        solved = False
        last_html_len = 0
        stable_count = 0
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            try:
                title = await page.evaluate("document.title")
                html = await page.get_content()
            except Exception:
                continue

            # CF challenge pages have these markers
            cf_markers = [
                "Just a moment",
                "cf_chl_opt",
                "challenge-platform",
                "cf-mitigated",
                "Attention Required",
            ]
            is_cf = any(m in (title or "") or m in (html or "")[:3000] for m in cf_markers)
            if not is_cf and len(html) > 2000:
                # Page loaded normally
                solved = True
                break
            # Check if HTML is stable (page stopped updating)
            if len(html) == last_html_len:
                stable_count += 1
                if stable_count >= 5 and not is_cf:
                    solved = True
                    break
            else:
                stable_count = 0
            last_html_len = len(html)

        if not solved:
            log.warning(f"nodriver: CF not solved for {url}")
            return None

        # Give a moment for any final redirects / cookies
        await asyncio.sleep(2)

        # Get final HTML
        try:
            html = await page.get_content()
        except Exception:
            html = ""

        # Get current URL
        try:
            final_url = page.url
        except Exception:
            final_url = url

        # Get cookies
        cookies_dict = {}
        try:
            cookies = await browser.cookies.get_all()
            for c in cookies:
                cookies_dict[c.name] = c.value
        except Exception as e:
            log.warning(f"nodriver: cookie extraction failed: {e}")

        # Get UA
        try:
            ua = await page.evaluate("navigator.userAgent")
        except Exception:
            ua = ""

        return {
            "html": html,
            "cookies": cookies_dict,
            "user_agent": ua or "",
            "url": final_url if isinstance(final_url, str) else url,
        }
    except Exception as e:
        log.warning(f"nodriver bypass failed: {e}")
        return None
    finally:
        if browser:
            try:
                browser.stop()
            except Exception:
                pass


def bypass_sync(url: str) -> Optional[dict]:
    """Synchronous wrapper around the async bypass."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_bypass(url))
        finally:
            loop.close()
    except RuntimeError:
        # Already in an event loop — make a new thread
        import threading

        result: dict = {}
        def _run():
            try:
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                result["r"] = new_loop.run_until_complete(_bypass(url))
                new_loop.close()
            except Exception as e:
                result["e"] = e

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=CF_WAIT_SEC + 15)
        if "e" in result:
            raise result["e"]
        return result.get("r")
