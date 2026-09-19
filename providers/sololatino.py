"""
SoloLatino provider: scrapes sololatino.net for movies and series in Spanish/Latino.

IMPORTANT: SoloLatino.net is behind Cloudflare Turnstile which blocks all
server-side requests from datacenter IPs. This provider ONLY works if you
have FlareSolverr running with a residential proxy.

Setup:
  1. Deploy FlareSolverr (Docker: flaresolverr/flaresolverr:latest)
  2. Configure it with a residential proxy (Webshare, BrightData, etc.)
  3. Set FLARESOLVERR_URL env var in this addon

Without FlareSolverr, all requests to sololatino.net will return 403.

URL structure (from decompiled APK):
  Catalog:  /peliculas/page/{N}/       (movies)
            /series/page/{N}/          (series)
            /genero/{slug}/            (by genre)
  Search:   /buscar?q={query}&page={N}
  Detail:   /pelicula/{slug}           (movie detail)
            /serie/{slug}              (series detail)
  Stream:   /api/player-url            (POST with {"t": playerToken})
            → returns {"url": "iframe_url"}
            → iframe page has PoW + AES encrypted dataLink
"""

import re
import base64
import json
import logging
import urllib3
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

from utils.embed69_solver import resolve_servers
from extractors.hosts import extract_stream

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

BASE_URL = "https://sololatino.net"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"}


def _get(path: str, referer: str = None) -> Optional[requests.Response]:
    """
    GET a path on sololatino.net. Uses FlareSolverr if available to bypass
    Cloudflare. Returns None if FlareSolverr is not configured or fails.
    """
    url = path if path.startswith("http") else urljoin(BASE_URL, path)

    # Try direct request first (fast, works if not behind CF or if CF is down)
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=15, verify=False)
        if r.status_code == 200 and "just a moment" not in r.text.lower()[:2000]:
            return r
    except Exception:
        pass

    # Try FlareSolverr
    try:
        from utils.flaresolverr import fetch_with_clearance
        r = fetch_with_clearance(url, referer=referer)
        if r and r.status_code == 200:
            return r
    except Exception as e:
        log.warning(f"FlareSolverr failed for {url}: {e}")

    log.info(f"SoloLatino: cannot access {url} (Cloudflare blocks, need FlareSolverr + residential proxy)")
    return None


# ============================================================
# Catalog scraping
# ============================================================
def _parse_card(card) -> Optional[dict]:
    """Parse a movie/series card. SoloLatino uses div.card with .card__poster, .card__title."""
    # Find the parent <a> tag with href
    a = card if card.name == "a" else card.find("a", href=True)
    if not a:
        return None
    href = a.get("href", "")
    if not href.startswith("http"):
        href = urljoin(BASE_URL, href)

    is_movie = "/pelicula/" in href
    is_series = "/serie/" in href
    if not (is_movie or is_series):
        return None

    m = re.search(r"/(?:pelicula|serie)/([^/?#]+)", href)
    if not m:
        return None
    slug = m.group(1)

    # Poster: img.card__poster src
    poster = None
    img = card.select_one("img.card__poster") or card.find("img")
    if img:
        poster = img.get("src") or img.get("data-src") or ""
        if poster and not poster.startswith("http"):
            poster = urljoin(BASE_URL, poster)

    # Title: .card__title
    title_el = card.select_one(".card__title")
    title = title_el.text.strip() if title_el else slug.replace("-", " ").title()

    # Year: .card__year
    year_el = card.select_one(".card__year")
    year = year_el.text.strip() if year_el else ""

    return {
        "id": f"sololatino:{slug}",
        "type": "movie" if is_movie else "series",
        "name": f"{title} ({year})" if year else title,
        "poster": poster or None,
        "slug": slug,
    }


def get_movies(page: int = 1) -> list[dict]:
    """Get a page of movies from SoloLatino."""
    path = f"/peliculas/page/{page}/" if page > 1 else "/peliculas/"
    r = _get(path)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []
    for card in soup.select("div.card"):
        item = _parse_card(card)
        if item and item["type"] == "movie" and item["slug"] not in seen:
            seen.add(item["slug"])
            out.append(item)
    return out


def get_series(page: int = 1) -> list[dict]:
    """Get a page of series from SoloLatino."""
    path = f"/series/page/{page}/" if page > 1 else "/series/"
    r = _get(path)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []
    for card in soup.select("div.card"):
        item = _parse_card(card)
        if item and item["type"] == "series" and item["slug"] not in seen:
            seen.add(item["slug"])
            out.append(item)
    return out


def search(query: str) -> list[dict]:
    """Search SoloLatino."""
    path = f"/buscar?q={quote(query)}"
    r = _get(path)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []
    for card in soup.select("div.card"):
        item = _parse_card(card)
        if item and item["slug"] not in seen:
            seen.add(item["slug"])
            out.append(item)
    return out


# ============================================================
# Stream resolution (PoW + AES, same as FlixLatam)
# ============================================================
def resolve_movie_streams(slug: str) -> list[dict]:
    """
    Resolve streams for a SoloLatino movie.
    
    Flow:
      1. GET /pelicula/{slug} → find player token + model + ID
      2. POST /api/player-url {"t": token} → get iframe URL
      3. GET iframe URL → page with PoW + dataLink
      4. Solve PoW + decrypt AES → get embed URLs
      5. Extract playable stream from each embed
    """
    r = _get(f"/pelicula/{slug}")
    if not r:
        return []

    # Find the player token, model, and ID
    # Pattern from APK: data attributes or JS variables
    token_match = re.search(r'playerToken\s*=\s*["\']([^"\']+)["\']', r.text)
    model_match = re.search(r'playerModel\s*=\s*["\']([^"\']+)["\']', r.text)
    id_match = re.search(r'playerId\s*=\s*["\']([^"\']+)["\']', r.text)

    if not (token_match and model_match and id_match):
        # Try alternative: look for /api/player-url/{model}/{id} pattern
        api_match = re.search(r'/api/player-url/(\w+)/(\w+)', r.text)
        if api_match:
            return _resolve_via_api(api_match.group(1), api_match.group(2), f"/pelicula/{slug}")
        log.debug(f"SoloLatino: no player token found for {slug}")
        return []

    token = token_match.group(1)
    model = model_match.group(1)
    player_id = id_match.group(1)

    return _resolve_via_api(model, player_id, f"/pelicula/{slug}", token)


def _resolve_via_api(model: str, player_id: str, referer: str, token: str = None) -> list[dict]:
    """POST to /api/player-url to get the iframe URL, then resolve streams."""
    headers = dict(HEADERS)
    headers["Referer"] = urljoin(BASE_URL, referer)
    headers["X-Requested-With"] = "XMLHttpRequest"
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json"

    # If we have a token, POST to /api/player-url
    if token:
        try:
            r = requests.post(
                f"{BASE_URL}/api/player-url",
                json={"t": token},
                headers=headers,
                timeout=15,
                verify=False,
            )
            if r.status_code == 419 or r.status_code == 403:
                # Need CSRF cookie first
                requests.get(f"{BASE_URL}/sanctum/csrf-cookie", headers=HEADERS, timeout=10, verify=False)
                r = requests.post(
                    f"{BASE_URL}/api/player-url",
                    json={"t": token},
                    headers=headers,
                    timeout=15,
                    verify=False,
                )
            if r.status_code == 200:
                data = r.json()
                iframe_url = data.get("url", "")
                if iframe_url:
                    return _process_iframe(iframe_url, referer)
        except Exception as e:
            log.warning(f"SoloLatino API call failed: {e}")

    # Fallback: GET /api/player-url/{model}/{id}
    try:
        r = requests.get(
            f"{BASE_URL}/api/player-url/{model}/{player_id}",
            headers=headers,
            timeout=15,
            verify=False,
        )
        if r.status_code == 200:
            data = r.json()
            iframe_url = data.get("url", "")
            if iframe_url:
                return _process_iframe(iframe_url, referer)
    except Exception as e:
        log.warning(f"SoloLatino API fallback failed: {e}")

    return []


def _process_iframe(iframe_url: str, referer: str) -> list[dict]:
    """Fetch the iframe page, solve PoW + AES, and return streams."""
    if not iframe_url.startswith("http"):
        iframe_url = urljoin(BASE_URL, iframe_url)

    headers = dict(HEADERS)
    headers["Referer"] = urljoin(BASE_URL, referer)

    try:
        r = requests.get(iframe_url, headers=headers, timeout=15, verify=False)
        if r.status_code != 200:
            return []
    except Exception as e:
        log.warning(f"SoloLatino iframe fetch failed: {e}")
        return []

    # Use the embed69_solver to resolve PoW + AES + dataLink
    servers = resolve_servers(r.text)

    streams = []
    for s in servers:
        try:
            playable = extract_stream(s["url"], s["host"])
        except Exception:
            playable = None
        if playable:
            streams.append({
                "name": f"StreamFlix {s['name']} (SoloLatino)",
                "title": f"SoloLatino • {s['name']}\nLatino/Castellano/Sub",
                "url": playable,
            })
    return streams


def is_available() -> bool:
    """Check if SoloLatino is accessible (requires FlareSolverr)."""
    r = _get("/")
    return r is not None
