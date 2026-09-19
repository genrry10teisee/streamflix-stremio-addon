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
    Verify that a stream URL actually works. STRICT mode.
    
    Returns True ONLY if the URL is confirmed working:
      - .m3u8: GET returns 200 AND response contains #EXTM3U
      - .mp4/other: HEAD or GET returns 200
    
    Returns False for:
      - Blacklisted URLs (test videos, Big Buck Bunny, etc.)
      - DNS errors, connection refused
      - Timeouts (too slow = won't work in Stremio)
      - HTTP 403/404/500 (broken or blocked)
      - Empty responses
    
    "chau chau y con otro" — if it doesn't pass, it's gone.
    """
    if not url or not url.startswith("http"):
        return False
    
    if is_blacklisted(url):
        log.info(f"✗ Blacklisted (test video): {url[:80]}")
        return False
    
    try:
        # For m3u8: must get 200 + #EXTM3U
        if ".m3u8" in url:
            r = requests.get(
                url,
                timeout=_VERIFY_TIMEOUT,
                stream=True,
                allow_redirects=True,
            )
            if r.status_code != 200:
                log.info(f"✗ m3u8 HTTP {r.status_code}: {url[:60]}")
                return False
            # Read first 500 bytes to check for #EXTM3U
            first_bytes = r.raw.read(500, decode_content=True)
            r.close()
            if not first_bytes:
                log.info(f"✗ m3u8 empty response: {url[:60]}")
                return False
            content_start = first_bytes.decode("utf-8", errors="ignore")
            if "#EXTM3U" not in content_start:
                log.info(f"✗ m3u8 no #EXTM3U marker: {url[:60]}")
                return False
            log.debug(f"✓ m3u8 valid: {url[:60]}")
            return True
        
        # For mp4 and other: HEAD first, then GET if needed
        else:
            r = requests.head(
                url,
                timeout=_VERIFY_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 200:
                # Check content-length if available
                cl = r.headers.get("content-length", "")
                if cl and int(cl) < 10000:
                    log.info(f"✗ Too small ({cl}b): {url[:60]}")
                    return False
                return True
            # HEAD not supported? Try GET
            if r.status_code in (403, 405, 501):
                r = requests.get(
                    url,
                    timeout=_VERIFY_TIMEOUT,
                    stream=True,
                    allow_redirects=True,
                )
                if r.status_code == 200:
                    cl = r.headers.get("content-length", "")
                    if cl and int(cl) < 10000:
                        log.info(f"✗ Too small ({cl}b): {url[:60]}")
                        return False
                    return True
            log.info(f"✗ HTTP {r.status_code}: {url[:60]}")
            return False
    except requests.exceptions.Timeout:
        log.info(f"✗ Timeout: {url[:60]}")
        return False
    except requests.exceptions.ConnectionError as e:
        log.info(f"✗ Connection error: {url[:60]} ({str(e)[:50]})")
        return False
    except Exception as e:
        log.info(f"✗ Verify error: {url[:60]} ({str(e)[:50]})")
        return False


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
