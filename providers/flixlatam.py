"""
FlixLatam provider: scrapes the public catalog and resolves streams.

FlixLatam uses TMDB images and IMDb IDs directly in its URLs, which makes
it a perfect match for Stremio (Stremio identifies content by IMDb ID).

URL structure:
  Catalog:  /peliculas?page=N         (24 movies per page)
            /series?page=N
            /peliculas/populares
  Detail:   /pelicula/{slug}         (slug = title-hash)
            /serie/{slug}
  Episode:  /serie/{slug}/temporada/{s}/capitulo/{e}
  Stream:   /vidurl/{imdb_id}/                       (movies)
            /vidurl/{imdb_id}-{season}x{episode:02d}/  (episodes)
"""

import re
import json
import logging
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

from utils.embed69_solver import resolve_servers
from extractors.hosts import extract_stream

log = logging.getLogger(__name__)

BASE_URL = "https://flixlatam.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"}


def _get(path: str, referer: Optional[str] = None) -> Optional[requests.Response]:
    """GET a path on flixlatam.com with proper headers."""
    url = path if path.startswith("http") else urljoin(BASE_URL, path)
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            log.debug(f"GET {url} -> {r.status_code}")
            return None
        return r
    except Exception as e:
        log.warning(f"GET {url} failed: {e}")
        return None


# ============================================================
# Catalog scraping
# ============================================================
def _parse_card(card) -> Optional[dict]:
    """Parse a single movie/series card from a listing page."""
    href = card.get("href")
    if not href:
        return None
    href = urljoin(BASE_URL, href)

    # Detect type
    is_movie = "/pelicula/" in href
    is_series = "/serie/" in href
    if not (is_movie or is_series):
        return None

    # Extract slug
    m = re.search(r"/(pelicula|serie)/([^/?#]+)", href)
    if not m:
        return None
    slug = m.group(2)

    # Poster URL
    img = card.find("img")
    poster = img.get("src") or img.get("data-src") if img else None

    # Title from alt
    alt = img.get("alt", "") if img else ""
    title = re.sub(r"^Ver\s+(.+?)\s+online$", r"\1", alt).strip() or slug

    return {
        "id": slug,  # Stremio ID = flixlatam slug (prefixed later)
        "type": "movie" if is_movie else "series",
        "name": title,
        "poster": poster,
        "slug": slug,
    }


def get_movies(page: int = 1) -> list[dict]:
    """Get a page of movies (24 per page)."""
    r = _get(f"/peliculas?page={page}")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []
    for a in soup.select("a[href*='/pelicula/']"):
        item = _parse_card(a)
        if item and item["slug"] not in seen:
            seen.add(item["slug"])
            out.append(item)
    return out


def get_series(page: int = 1) -> list[dict]:
    """Get a page of series (24 per page)."""
    r = _get(f"/series?page={page}")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []
    for a in soup.select("a[href*='/serie/']"):
        # Skip episode links
        href = a.get("href", "")
        if "/temporada/" in href or "/capitulo/" in href:
            continue
        item = _parse_card(a)
        if item and item["slug"] not in seen:
            seen.add(item["slug"])
            out.append(item)
    return out


def get_popular_movies(page: int = 1) -> list[dict]:
    """Get popular movies page."""
    r = _get(f"/peliculas/populares?page={page}")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []
    for a in soup.select("a[href*='/pelicula/']"):
        item = _parse_card(a)
        if item and item["slug"] not in seen:
            seen.add(item["slug"])
            out.append(item)
    return out


# ============================================================
# Detail page
# ============================================================
def get_movie_detail(slug: str) -> Optional[dict]:
    """
    Get movie detail: title, poster, description, genres, imdb_id.

    Returns None if not found.
    """
    r = _get(f"/pelicula/{slug}")
    if not r:
        return None

    # Extract IMDb ID
    imdb_ids = re.findall(r"tt\d{7,10}", r.text)
    imdb_id = imdb_ids[0] if imdb_ids else None

    # Parse JSON-LD
    soup = BeautifulSoup(r.text, "html.parser")
    info = {"slug": slug, "imdb_id": imdb_id, "type": "movie"}
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string)
            if data.get("@type") == "Movie":
                info["name"] = data.get("name", slug)
                info["description"] = data.get("description", "")
                info["poster"] = data.get("image", "")
                info["genres"] = data.get("genre", [])
                if isinstance(info["genres"], str):
                    info["genres"] = [info["genres"]]
                break
        except Exception:
            continue

    # Fallback: meta tags
    if "name" not in info:
        og = soup.find("meta", property="og:title")
        if og:
            info["name"] = re.sub(r"\s*-\s*FLIXLATAM\s*$", "", og.get("content", ""))
    if "poster" not in info:
        og = soup.find("meta", property="og:image")
        if og:
            info["poster"] = og.get("content", "")
    if "description" not in info:
        og = soup.find("meta", property="og:description")
        if og:
            info["description"] = og.get("content", "")

    return info if "imdb_id" in info and info["imdb_id"] else None


def get_series_detail(slug: str) -> Optional[dict]:
    """
    Get series detail: title, poster, description, genres, seasons, episodes.

    Returns:
        {
            "slug": str,
            "imdb_id": str or None,
            "type": "series",
            "name": str,
            "description": str,
            "poster": str,
            "genres": list[str],
            "seasons": { season_num: [{"episode": int, "imdb_id": str, "vidurl_path": str}] }
        }
    """
    r = _get(f"/serie/{slug}")
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    info = {"slug": slug, "type": "series", "seasons": {}}
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string)
            if data.get("@type") == "TVSeries":
                info["name"] = data.get("name", slug)
                info["description"] = data.get("description", "")
                info["poster"] = data.get("image", "")
                info["genres"] = data.get("genre", [])
                if isinstance(info["genres"], str):
                    info["genres"] = [info["genres"]]
                break
        except Exception:
            continue

    # Fallback meta tags
    if "name" not in info:
        og = soup.find("meta", property="og:title")
        if og:
            info["name"] = re.sub(r"\s*-\s*FLIXLATAM\s*$", "", og.get("content", ""))
    if "poster" not in info:
        og = soup.find("meta", property="og:image")
        if og:
            info["poster"] = og.get("content", "")

    # Find all episode links (only fetch the page once, parse all seasons/episodes from links)
    # Pattern: /serie/{slug}/temporada/{s}/capitulo/{e}
    ep_pattern = re.compile(
        rf"/serie/{re.escape(slug)}/temporada/(\d+)/capitulo/(\d+)"
    )
    episodes_by_season = {}
    for m in ep_pattern.finditer(r.text):
        season = int(m.group(1))
        episode = int(m.group(2))
        episodes_by_season.setdefault(season, []).append(episode)

    # For each episode, we'd need to fetch its page to get its IMDb ID.
    # That's too many requests for catalog browsing; instead, we resolve
    # on-demand in get_episode_imdb_id().
    for season, episodes in episodes_by_season.items():
        info["seasons"][season] = sorted(set(episodes))

    return info


def get_episode_imdb_id(slug: str, season: int, episode: int) -> Optional[str]:
    """
    Fetch a specific episode page and extract its IMDb ID + vidurl path.
    """
    r = _get(f"/serie/{slug}/temporada/{season}/capitulo/{episode}")
    if not r:
        return None

    # Find vidurl path: /vidurl/ttXXXX-1x01/
    m = re.search(r"/vidurl/(tt\d{7,10}-\d+x\d+)/", r.text)
    if m:
        return m.group(1)

    # Fallback: just look for tt###
    ids = re.findall(r"tt\d{7,10}", r.text)
    if ids:
        # Construct the vidurl path manually
        return f"{ids[0]}-{season}x{episode:02d}"
    return None


# ============================================================
# Stream resolution
# ============================================================
def resolve_movie_streams(imdb_id: str) -> list[dict]:
    """
    Given an IMDb ID, fetch the player page and resolve streams.
    Returns a list of Stremio stream dicts:
        [{"name": ..., "title": ..., "url": playable_url}]
    """
    vidurl = f"/vidurl/{imdb_id}/"
    r = _get(vidurl, referer=f"{BASE_URL}/pelicula/x")
    if not r:
        return []

    servers = resolve_servers(r.text)
    return _servers_to_stremio(servers)


def resolve_episode_streams(imdb_id: str, season: int, episode: int) -> list[dict]:
    """
    Given an IMDb ID + season/episode, fetch the player page and resolve streams.
    """
    vidurl_id = f"{imdb_id}-{season}x{episode:02d}"
    vidurl = f"/vidurl/{vidurl_id}/"
    r = _get(vidurl, referer=f"{BASE_URL}/serie/x")
    if not r:
        return []

    servers = resolve_servers(r.text)
    return _servers_to_stremio(servers)


def _servers_to_stremio(servers: list[dict]) -> list[dict]:
    """
    Convert resolved embed servers into Stremio stream entries.
    For each server, try to extract the playable URL. If extraction fails,
    skip that server (Stremio can't render arbitrary iframes).
    """
    streams = []
    for s in servers:
        try:
            playable = extract_stream(s["url"], s["host"])
        except Exception as e:
            log.warning(f"extract_stream failed for {s['url']}: {e}")
            playable = None

        if playable:
            streams.append({
                "name": f"StreamFlix {s['name']}".strip(),
                "title": f"FlixLatam • {s['name']}\nLatino/Castellano/Sub",
                "url": playable,
            })
    return streams
