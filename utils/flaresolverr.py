"""
Optional FlareSolverr client for bypassing Cloudflare challenges.

FlareSolverr is a standalone service that runs a headless browser to solve
Cloudflare's JavaScript challenges and returns the cookies + user-agent
needed to make authenticated requests.

Deploy FlareSolverr separately (e.g. on Render free tier or a small VPS)
and set the FLARESOLVERR_URL env var to its endpoint, e.g.:
    FLARESOLVERR_URL=https://my-flaresolverr.onrender.com

If FLARESOLVERR_URL is not set, this module is a no-op (returns None) and
the addon only works with providers that don't use Cloudflare (flixlatam).
"""

import os
import json
import logging
from typing import Optional
import requests

log = logging.getLogger(__name__)

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "").rstrip("/")


def is_enabled() -> bool:
    """Return True if FlareSolverr is configured."""
    return bool(FLARESOLVERR_URL)


def solve(url: str, max_timeout: int = 60) -> Optional[dict]:
    """
    Use FlareSolverr to bypass Cloudflare on the given URL.

    Returns:
        {
            "url": final URL after redirects,
            "html": page HTML,
            "cookies": dict of cookies (including cf_clearance),
            "user_agent": UA used by the headless browser,
        }
        or None if FlareSolverr is unavailable / failed.
    """
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
            log.warning(f"FlareSolverr returned {r.status_code}")
            return None
        data = r.json()
        if data.get("status") != "ok":
            log.warning(f"FlareSolverr error: {data.get('message', '?')}")
            return None
        sol = data.get("solution", {})
        # Parse cookies into a dict
        cookies = {c["name"]: c["value"] for c in sol.get("cookies", [])}
        return {
            "url": sol.get("url", url),
            "html": sol.get("response", ""),
            "cookies": cookies,
            "user_agent": sol.get("userAgent", ""),
        }
    except Exception as e:
        log.warning(f"FlareSolverr request failed: {e}")
        return None


def fetch_with_clearance(url: str, referer: Optional[str] = None) -> Optional[requests.Response]:
    """
    Fetch a Cloudflare-protected URL via FlareSolverr and return a fake
    Response object compatible with the rest of the codebase.
    """
    sol = solve(url)
    if not sol:
        return None
    # Build a fake Response
    fake = requests.Response()
    fake.status_code = 200
    fake._content = sol["html"].encode("utf-8")
    fake.url = sol["url"]
    fake.headers["Content-Type"] = "text/html; charset=utf-8"
    # Set cookies on the response
    for name, value in sol["cookies"].items():
        fake.cookies.set(name, value)
    return fake
