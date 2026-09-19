"""
Fanpelis provider: uses the public REST API at https://fanpelis.to/api/rest/

Endpoints:
  GET /api/rest/listing?page=1&post_type=movies&posts_per_page=20
  GET /api/rest/search?query=X&page=1&post_type=movies&posts_per_page=20
  GET /api/rest/single?post_name=slug&post_type=movies
  GET /api/rest/episodes?post_id=ID  (for tvshows)
  GET /api/rest/player?post_id=ID&_any=1  (returns embed URLs)

Valid post_type values: "movies", "tvshows"
"""

import re
import logging
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from extractors.hosts import extract_stream

log = logging.getLogger(__name__)

BASE_URL = "https://fanpelis.to"
API_URL = "https://fanpelis.to/api/rest"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json", "Referer": f"{BASE_URL}/"}


def _get_api(endpoint: str, params: dict = None) -> Optional[dict]:
    """Call a Fanpelis API endpoint and return parsed JSON."""
    try:
        r = requests.get(f"{API_URL}/{endpoint}", headers=HEADERS, params=params, timeout=15, verify=False)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        log.warning(f"Fanpelis API {endpoint} failed: {e}")
        return None


def _parse_post(post: dict, item_type: str) -> dict:
    """Convert API post to internal format."""
    images = post.get("images", {}) or {}
    poster = images.get("poster", "") if isinstance(images, dict) else ""
    if poster and not poster.startswith("http"):
        poster = urljoin(BASE_URL, poster)
    return {
        "id": f"fanpelis:{post.get('slug','')}",
        "type": item_type,
        "name": post.get("title", ""),
        "poster": poster or None,
        "slug": post.get("slug", ""),
        "post_id": post.get("_id"),
        "description": post.get("overview", ""),
        "rating": post.get("rating", ""),
        "release_date": post.get("release_date", ""),
        "runtime": post.get("runtime", ""),
    }


def get_movies(page: int = 1, query: str = "") -> list[dict]:
    """Get a page of movies."""
    if query:
        data = _get_api("search", {"query": query, "page": page, "post_type": "movies", "posts_per_page": 20})
    else:
        data = _get_api("listing", {"page": page, "post_type": "movies", "posts_per_page": 20})
    if not data or data.get("error"):
        return []
    posts = data.get("data", {}).get("posts", [])
    return [_parse_post(p, "movie") for p in posts]


def get_tvshows(page: int = 1, query: str = "") -> list[dict]:
    """Get a page of TV shows."""
    if query:
        data = _get_api("search", {"query": query, "page": page, "post_type": "tvshows", "posts_per_page": 20})
    else:
        data = _get_api("listing", {"page": page, "post_type": "tvshows", "posts_per_page": 20})
    if not data or data.get("error"):
        return []
    posts = data.get("data", {}).get("posts", [])
    return [_parse_post(p, "series") for p in posts]


def search(query: str, item_type: str = "movies") -> list[dict]:
    """Search Fanpelis."""
    if item_type == "series":
        return get_tvshows(page=1, query=query)
    return get_movies(page=1, query=query)


def get_movie_detail(slug: str) -> Optional[dict]:
    """Get movie detail."""
    data = _get_api("single", {"post_name": slug, "post_type": "movies"})
    if not data or data.get("error"):
        return None
    post = data.get("data", {})
    info = _parse_post(post, "movie")
    info["genres"] = []  # API returns genre IDs, not names
    return info


def get_tvshow_detail(slug: str) -> Optional[dict]:
    """Get TV show detail. We need the post_id first."""
    data = _get_api("single", {"post_name": slug, "post_type": "tvshows"})
    if not data or data.get("error"):
        return None
    post = data.get("data", {})
    info = _parse_post(post, "series")
    info["post_id"] = post.get("_id")
    info["seasons"] = {}  # Will be fetched on-demand
    return info


def get_episodes(post_id: int) -> list[dict]:
    """Get episodes for a TV show."""
    data = _get_api("episodes", {"post_id": post_id})
    if not data or data.get("error"):
        return []
    episodes = data.get("data", [])
    if not isinstance(episodes, list):
        return []
    return episodes


def resolve_streams(post_id: int) -> list[dict]:
    """Resolve streams for a movie or episode."""
    data = _get_api("player", {"post_id": post_id, "_any": 1})
    if not data or data.get("error"):
        return []
    embeds = data.get("data", {}).get("embeds", [])
    streams = []
    for embed in embeds:
        url = embed.get("url", "")
        lang = embed.get("lang", "")
        quality = embed.get("quality", "")
        if not url:
            continue
        # Try to extract playable URL
        try:
            playable = extract_stream(url)
        except Exception as e:
            log.warning(f"extract_stream failed for {url}: {e}")
            playable = None
        if playable:
            streams.append({
                "name": f"StreamFlix [{quality[:6]}]",
                "title": f"Fanpelis • {lang} {quality}",
                "url": playable,
            })
    return streams
