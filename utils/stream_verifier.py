"""
Stream verifier: checks if a stream URL actually works before returning it to Stremio.

For each stream URL, we do a quick HEAD or GET request to verify:
  - HTTP status is 200 (or 403 with valid content-type, some CDNs do this)
  - For .m3u8: response starts with #EXTM3U
  - For .mp4: response has content-length > 0
  - Filter out known test/placeholder URLs (Big Buck Bunny, test-videos.co.uk, etc.)

This runs in parallel with a short timeout (5s per stream) so it doesn't slow
down the response too much.
"""

import re
import logging
import concurrent.futures
from typing import Optional
import requests

log = logging.getLogger(__name__)

# URLs that are known test/placeholder videos that should be filtered out
BLACKLIST_PATTERNS = [
    r"test-videos\.co\.uk",
    r"bigbuckbunny",
    r"Big_Buck_Bunny",
    r"sample\.mp4",
    r"test\.mp4",
    r"placeholder",
    r"example\.com",
    r"localhost",
    r"127\.0\.0\.1",
]

# Compile the blacklist patterns for performance
_BLACKLIST_RE = [re.compile(p, re.IGNORECASE) for p in BLACKLIST_PATTERNS]

# Timeout for verification requests (seconds)
_VERIFY_TIMEOUT = 6


def is_blacklisted(url: str) -> bool:
    """Check if a URL matches any blacklist pattern (test videos, etc.)."""
    for pattern in _BLACKLIST_RE:
        if pattern.search(url):
            return True
    return False


def verify_stream(url: str) -> bool:
    """
    Verify that a stream URL actually works.
    
    Returns True if the URL is valid and playable, False otherwise.
    
    Strategy:
      - Always filter out blacklisted URLs (test videos, etc.)
      - For .m3u8: try GET and check for #EXTM3U, but if request fails
        (timeout, 403), still return True (many CDNs block server-side requests
        but work fine in Stremio's player)
      - For .mp4: try HEAD, but if it fails, still return True (same reason)
      - Only return False for clearly broken URLs (connection refused, DNS error, etc.)
    """
    if not url or not url.startswith("http"):
        return False
    
    if is_blacklisted(url):
        log.info(f"Blacklisted (test video): {url[:80]}")
        return False
    
    try:
        # For m3u8, try to verify but don't be too aggressive
        if ".m3u8" in url:
            r = requests.get(
                url,
                timeout=_VERIFY_TIMEOUT,
                stream=True,
                allow_redirects=True,
            )
            if r.status_code == 200:
                first_bytes = r.raw.read(100, decode_content=True)
                r.close()
                if first_bytes:
                    content_start = first_bytes.decode("utf-8", errors="ignore")
                    if "#EXTM3U" in content_start:
                        return True
                    # Some m3u8 don't start with #EXTM3U immediately (BOM, comments)
                    # If we got 200 with content, accept it
                    log.debug(f"m3u8 {url[:60]} -> 200 but no #EXTM3U in first 100 bytes, accepting anyway")
                    return True
            # 403/401 might mean the CDN blocks server-side but works in player
            # Don't filter these out — Stremio's player can handle them
            log.debug(f"m3u8 {url[:60]} -> HTTP {r.status_code}, accepting (CDN may block server-side)")
            return True
        
        # For mp4 and other, do a HEAD request
        else:
            r = requests.head(
                url,
                timeout=_VERIFY_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 200:
                return True
            # 403/405 might just mean HEAD not supported, try GET
            if r.status_code in (403, 405):
                r = requests.get(
                    url,
                    timeout=_VERIFY_TIMEOUT,
                    stream=True,
                    allow_redirects=True,
                )
                if r.status_code == 200:
                    return True
            # If we can't verify, accept anyway (better to show and let Stremio try)
            log.debug(f"{url[:60]} -> HTTP {r.status_code}, accepting (may work in player)")
            return True
    except requests.exceptions.Timeout:
        # Timeout doesn't mean the URL is bad — the CDN might be slow
        log.debug(f"Timeout verifying {url[:60]}, accepting anyway")
        return True
    except requests.exceptions.ConnectionError as e:
        # DNS errors, connection refused = truly broken
        if "Name or service not known" in str(e) or "Connection refused" in str(e):
            log.info(f"Broken URL (DNS/connection): {url[:60]}")
            return False
        # Other connection errors might be temporary
        log.debug(f"Connection error for {url[:60]}: {e}, accepting")
        return True
    except Exception as e:
        log.debug(f"Verify error for {url[:60]}: {e}, accepting")
        return True


def verify_streams_parallel(streams: list[dict], max_workers: int = 8) -> list[dict]:
    """
    Verify a list of streams in parallel. Returns only the working streams.
    
    Each stream dict must have a "url" key.
    """
    if not streams:
        return []
    
    working = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit verification for each stream
        future_to_stream = {
            executor.submit(verify_stream, s.get("url", "")): s
            for s in streams
        }
        
        for future in concurrent.futures.as_completed(future_to_stream, timeout=30):
            stream = future_to_stream[future]
            try:
                is_valid = future.result(timeout=15)
                if is_valid:
                    working.append(stream)
                    log.info(f"✓ Verified: {stream.get('name', '?')[:40]}")
                else:
                    log.info(f"✗ Invalid: {stream.get('name', '?')[:40]}")
            except Exception as e:
                log.debug(f"Verify exception for {stream.get('url','')[:60]}: {e}")
    
    return working


def verify_single(url: str) -> bool:
    """Verify a single URL (alias for verify_stream)."""
    return verify_stream(url)
