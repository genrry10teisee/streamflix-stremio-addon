"""
Latanime provider: scrapes latanime.org for anime series in Spanish/Latino.

URL structure:
  Catalog:  /                                    (home with featured)
            /directorio                          (full catalog)
            /directorio?q=QUERY                  (search)
  Detail:   /anime/{slug}                        (series detail + episodes)
  Episode:  /ver/{slug}-episodio-{N}             (episode with embeds)

Episode pages contain multiple `<a data-player="base64-encoded-url">` elements
pointing to different video hosts (dsvplay, bysekoze, hexload, savefiles,
mega, mixdrop, voe, mp4upload).

Note: latanime.org does NOT use IMDb IDs. Episodes are identified by their
URL slug. To integrate with Stremio (which uses IMDb IDs), we use a custom
ID format: `latanime:{slug}` for series and `latanime:{slug}:{episode}` for
episodes. Stremio's Cinemeta won't find these by name, so users need to
browse the catalog directly to discover anime.
"""

import re
import base64
import logging
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

from extractors.hosts import extract_stream

log = logging.getLogger(__name__)

BASE_URL = "https://latanime.org"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"}


def _get(path: str, referer: Optional[str] = None) -> Optional[requests.Response]:
    """GET a path on latanime.org with proper headers."""
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
def _parse_anime_card(card) -> Optional[dict]:
    """Parse a single anime card from a listing page."""
    href = card.get("href")
    if not href:
        return None
    href = urljoin(BASE_URL, href)
    if "/anime/" not in href:
        return None

    # Extract slug
    m = re.search(r"/anime/([^/?#]+)", href)
    if not m:
        return None
    slug = m.group(1)

    # Poster
    img = card.find("img")
    poster = None
    if img:
        poster = img.get("src") or img.get("data-src") or ""
        if poster and not poster.startswith("http"):
            poster = urljoin(BASE_URL, poster)

    # Title (from img alt or card title attr or text)
    title = ""
    if img:
        title = img.get("alt", "").strip()
    if not title:
        title = card.get("title", "").strip()
    if not title:
        # Try text content
        title_el = card.find(class_=re.compile("title|name", re.I))
        if title_el:
            title = title_el.text.strip()
    if not title:
        title = slug.replace("-", " ").title()

    return {
        "id": f"latanime:{slug}",
        "type": "series",
        "name": title,
        "poster": poster or None,
        "slug": slug,
    }


def get_anime_list(page: int = 1, query: str = "") -> list[dict]:
    """Get a page of anime from the /animes endpoint."""
    path = "/animes"
    if query:
        path += f"?q={quote(query)}"
    if page > 1:
        # Latanime uses ?p=N for pagination (separate from ?q=)
        sep = "&" if query else "?"
        path += f"{sep}p={page}"

    r = _get(path)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []

    # Try multiple selectors for anime cards
    cards = soup.select("a[href*='/anime/']")
    for c in cards:
        # Skip non-card links (navigation, etc.)
        if c.find("img"):
            item = _parse_anime_card(c)
            if item and item["slug"] not in seen:
                seen.add(item["slug"])
                out.append(item)
    return out


def get_home_anime() -> list[dict]:
    """Get anime from the home page (featured/recent)."""
    r = _get("/")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []
    for a in soup.select("a[href*='/anime/']"):
        if a.find("img"):
            item = _parse_anime_card(a)
            if item and item["slug"] not in seen:
                seen.add(item["slug"])
                out.append(item)
    return out


def search_anime(query: str) -> list[dict]:
    """Search anime by name."""
    return get_anime_list(page=1, query=query)


# ============================================================
# Detail page
# ============================================================
def get_anime_detail(slug: str) -> Optional[dict]:
    """
    Get anime detail: title, poster, description, genres, episodes.

    Returns:
        {
            "slug": str,
            "type": "series",
            "name": str,
            "description": str,
            "poster": str,
            "genres": list[str],
            "episodes": [{ "episode": int, "url": str, "season": int }],
        }
    """
    r = _get(f"/anime/{slug}")
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    info = {"slug": slug, "type": "series", "episodes": []}

    # Title
    title = soup.find("title")
    if title:
        # Format: "Anime Name — Latanime"
        info["name"] = re.sub(r"\s*[—-]\s*Latanime\s*$", "", title.text).strip()
    else:
        info["name"] = slug.replace("-", " ").title()

    # Poster
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "portada" in src or "imagen" in src or "thumbs" in src:
            if not src.startswith("http"):
                src = urljoin(BASE_URL, src)
            info["poster"] = src
            break

    # Description
    desc = soup.find("p", class_=re.compile("sinop|description|storyline|text"))
    if desc:
        info["description"] = desc.text.strip()

    # Genres
    genres = []
    for g in soup.select("a[href*='/genero/'], a[href*='/category/'], a[href*='/tag/']"):
        txt = g.text.strip()
        if txt and len(txt) < 30 and txt not in genres:
            genres.append(txt)
    info["genres"] = genres

    # Episodes: /ver/{slug}-episodio-{N}
    ep_pattern = re.compile(rf"/ver/({re.escape(slug)}-episodio-(\d+))")
    episodes_seen = set()
    for m in ep_pattern.finditer(r.text):
        ep_url = urljoin(BASE_URL, f"/ver/{m.group(1)}")
        ep_num = int(m.group(2))
        if ep_num not in episodes_seen:
            episodes_seen.add(ep_num)
            info["episodes"].append({
                "episode": ep_num,
                "season": 1,  # Latanime doesn't always have seasons; default to 1
                "url": ep_url,
            })

    # Sort episodes
    info["episodes"].sort(key=lambda e: e["episode"])
    return info


# ============================================================
# Episode stream resolution
# ============================================================
def get_episode_streams(slug: str, episode: int) -> list[dict]:
    """
    Get all streams for a specific episode.

    Returns:
        [{"name": "StreamFlix mixdrop", "title": ..., "url": playable_url}]
    """
    # Try the standard URL pattern
    ep_path = f"/ver/{slug}-episodio-{episode}"
    r = _get(ep_path)
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    # Find all <a class="play-video" data-player="base64...">
    streams = []
    seen_urls = set()
    for a in soup.select("a[data-player]"):
        enc = a.get("data-player", "")
        name = a.text.strip().lower()
        if not enc:
            continue
        try:
            embed_url = base64.b64decode(enc).decode("utf-8").strip()
        except Exception:
            continue

        if not embed_url or embed_url in seen_urls:
            continue
        seen_urls.add(embed_url)

        # Try to extract playable URL
        try:
            playable = extract_stream(embed_url, name)
        except Exception as e:
            log.warning(f"extract_stream failed for {embed_url}: {e}")
            playable = None

        if playable:
            streams.append({
                "name": f"StreamFlix {name}",
                "title": f"Latanime • {name}\nAnime en Castellano/Latino",
                "url": playable,
            })

    return streams
