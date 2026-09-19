"""
CineCalidad provider: scrapes cinecalidad.ec (redirects to cinecalidad.am)
for movies in Spanish/Latino.

URL structure:
  Catalog:  https://www.cinecalidad.ec/                       (home, latest movies)
            https://www.cinecalidad.ec/peliculas/page/N/      (paginated)
  Detail:   https://www.cinecalidad.am/ver-pelicula/{slug}/   (movie detail)
  Stream:   Detail page contains https://videoapp.zip/e/movie/{ID}
            → videoapp.zip page contains iframe to vimeos.net
            → vimeos.net embed page is resolved by extract_vimeos()
"""

import re
import logging
import urllib3
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

from extractors.hosts import extract_stream

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

BASE_URL = "https://www.cinecalidad.ec"
FALLBACK_URL = "https://www.cinecalidad.am"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"}


def _get(path: str, base: str = None, referer: str = None) -> Optional[requests.Response]:
    """GET a path from CineCalidad. Tries .ec first, falls back to .am."""
    bases = [base] if base else [BASE_URL, FALLBACK_URL]
    for b in bases:
        url = path if path.startswith("http") else urljoin(b, path)
        headers = dict(HEADERS)
        if referer:
            headers["Referer"] = referer
        try:
            r = requests.get(url, headers=headers, timeout=15, verify=False, allow_redirects=True)
            if r.status_code == 200:
                return r
        except Exception as e:
            log.debug(f"GET {url} failed: {e}")
    return None


# ============================================================
# Catalog scraping
# ============================================================
def _parse_movie_card(card) -> Optional[dict]:
    href = card.get("href")
    if not href:
        return None
    if not href.startswith("http"):
        href = urljoin(BASE_URL, href)
    if "/ver-pelicula/" not in href:
        return None
    m = re.search(r"/ver-pelicula/([^/?#]+)", href)
    if not m:
        return None
    slug = m.group(1)
    img = card.find("img")
    poster = None
    title = ""
    if img:
        poster = img.get("src") or img.get("data-src") or ""
        title = img.get("alt", "").strip()
    if not title:
        title = slug.replace("-", " ").title()
    # Clean title (remove "online gratis en cinecalidad" suffix)
    title = re.sub(r"\s*online.*$", "", title, flags=re.I).strip()
    return {
        "id": f"cinecalidad:{slug}",
        "type": "movie",
        "name": title,
        "poster": poster or None,
        "slug": slug,
    }


def get_movies(page: int = 1, query: str = "") -> list[dict]:
    """Get a page of movies from CineCalidad."""
    if query:
        path = f"/?s={quote(query)}"
    else:
        path = "/peliculas/" if page <= 1 else f"/peliculas/page/{page}/"
    
    r = _get(path)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []
    for a in soup.select("a[href*='/ver-pelicula/']"):
        if a.find("img"):
            item = _parse_movie_card(a)
            if item and item["slug"] not in seen:
                seen.add(item["slug"])
                out.append(item)
    return out


def search_movies(query: str) -> list[dict]:
    """Search movies by title."""
    return get_movies(page=1, query=query)


# ============================================================
# Stream resolution
# ============================================================
def resolve_movie_streams(slug: str) -> list[dict]:
    """
    Resolve streams for a CineCalidad movie.
    
    Flow:
      1. GET /ver-pelicula/{slug}/ → find videoapp.zip URL
      2. GET videoapp.zip page → find vimeos.net iframe URL
      3. Extract stream from vimeos.net (using existing extractor)
    """
    r = _get(f"/ver-pelicula/{slug}/")
    if not r:
        return []
    
    # Find videoapp.zip URL
    videoapp_match = re.search(r'(https?://videoapp\.zip/e/movie/\d+)', r.text)
    if not videoapp_match:
        log.debug(f"No videoapp.zip URL found for {slug}")
        return []
    
    videoapp_url = videoapp_match.group(1)
    log.info(f"CineCalidad {slug}: videoapp.zip URL = {videoapp_url}")
    
    # Fetch the videoapp.zip page to find the vimeos.net iframe
    try:
        r2 = requests.get(
            videoapp_url,
            headers={"User-Agent": UA, "Referer": r.url},
            timeout=15,
            verify=False,
        )
        if r2.status_code != 200:
            log.debug(f"videoapp.zip returned {r2.status_code}")
            return []
    except Exception as e:
        log.warning(f"videoapp.zip fetch failed: {e}")
        return []
    
    # Find iframe src (vimeos.net, voe.sx, doodstream, etc.)
    iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', r2.text)
    if not iframe_match:
        log.debug(f"No iframe found in videoapp.zip page")
        return []
    
    embed_url = iframe_match.group(1)
    log.info(f"CineCalidad {slug}: embed URL = {embed_url[:80]}")
    
    # Extract the playable stream from the embed URL
    try:
        playable = extract_stream(embed_url)
    except Exception as e:
        log.warning(f"extract_stream failed for {embed_url}: {e}")
        playable = None
    
    if playable:
        return [{
            "name": "StreamFlix [LAT] HD (CineCalidad)",
            "title": f"CineCalidad • Latino HD\n{slug.replace('-', ' ').title()}",
            "url": playable,
        }]
    
    return []
