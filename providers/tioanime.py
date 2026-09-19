"""
TioAnime provider: scrapes https://tioanime.com for anime in Spanish.

URL structure:
  Catalog:  /directorio                              (full catalog)
            /directorio?q=QUERY                      (search)
  Detail:   /anime/{slug}                            (anime detail + episode list)
  Episode:  /ver/{slug}-{N}                          (episode page)

Episode pages have `var videos = [["Server1", "url1", 0, 0], ...]` directly
in the HTML. Each video URL is an embed URL to a host (Voe, YourUpload, Mega, etc.).
"""

import re
import json
import logging
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

from extractors.hosts import extract_stream

log = logging.getLogger(__name__)

BASE_URL = "https://tioanime.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"}


def _get(path: str, referer: Optional[str] = None) -> Optional[requests.Response]:
    url = path if path.startswith("http") else urljoin(BASE_URL, path)
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=15, verify=False)
        if r.status_code != 200:
            return None
        return r
    except Exception as e:
        log.warning(f"GET {url} failed: {e}")
        return None


# ============================================================
# Catalog
# ============================================================
def _parse_anime_card(card) -> Optional[dict]:
    href = card.get("href")
    if not href:
        return None
    if not href.startswith("http"):
        href = urljoin(BASE_URL, href)
    m = re.search(r"/anime/([^/?#]+)", href)
    if not m:
        return None
    slug = m.group(1)
    img = card.find("img")
    poster = None
    title = ""
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("http"):
            src = urljoin(BASE_URL, src)
        poster = src
        title = img.get("alt", "").strip()
        if not title:
            # Try title attribute
            title = img.get("title", "").strip()
    if not title:
        # Try sibling text (TioAnime uses <h3 class="title"> or .overlay title)
        parent = card.parent if hasattr(card, "parent") else None
        if parent:
            title_el = parent.find(["h3", "h2", "span"], class_=re.compile("title|name"))
            if title_el:
                title = title_el.text.strip()
    if not title:
        # Try the card's own text
        title = card.text.strip().split("\n")[0][:50]
    if not title or title == "img":
        # Final fallback: slug
        title = slug.replace("-", " ").title()
    return {
        "id": f"tioanime:{slug}",
        "type": "series",
        "name": title,
        "poster": poster or None,
        "slug": slug,
    }


def get_anime_list(page: int = 1, query: str = "") -> list[dict]:
    """Get a page of anime from the /directorio endpoint."""
    path = "/directorio"
    if query:
        path += f"?q={quote(query)}"
    elif page > 1:
        path += f"?page={page}"

    r = _get(path)
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
    return get_anime_list(page=1, query=query)


# ============================================================
# Detail page
# ============================================================
def get_anime_detail(slug: str) -> Optional[dict]:
    """Get anime detail. Returns episodes from /anime/{slug} page."""
    r = _get(f"/anime/{slug}")
    if not r:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    info = {"slug": slug, "type": "series", "episodes": []}

    # Title from <h1 class="title"> or page title
    h1 = soup.find("h1", class_="title")
    if h1:
        info["name"] = h1.text.strip()
    else:
        t = soup.find("title")
        info["name"] = re.sub(r"\s*-\s*TioAnime\s*$", "", t.text if t else slug).strip()

    # Poster
    img = soup.select_one(".anime-poster img, .thumb img, .anime-img img, header img")
    if img:
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("http"):
            src = urljoin(BASE_URL, src)
        info["poster"] = src

    # Description
    desc = soup.find("p", class_=re.compile("sinop|description|storyline"))
    if desc:
        info["description"] = desc.text.strip()

    # Genres
    genres = []
    for g in soup.select("a[href*='/genero/'], a[href*='/genre/']"):
        txt = g.text.strip()
        if txt and len(txt) < 30 and txt not in genres:
            genres.append(txt)
    info["genres"] = genres

    # Episodes - pattern: /ver/{slug}-{N}
    # TioAnime lists episodes in a list
    ep_pattern = re.compile(rf"/ver/{re.escape(slug)}-(\d+)")
    episodes_seen = set()
    for m in ep_pattern.finditer(r.text):
        ep_num = int(m.group(1))
        if ep_num not in episodes_seen:
            episodes_seen.add(ep_num)
            info["episodes"].append({
                "episode": ep_num,
                "season": 1,
                "url": urljoin(BASE_URL, f"/ver/{slug}-{ep_num}"),
            })
    info["episodes"].sort(key=lambda e: e["episode"])
    return info


# ============================================================
# Episode stream resolution
# ============================================================
def get_episode_streams(slug: str, episode: int) -> list[dict]:
    """Get all streams for a specific episode."""
    r = _get(f"/ver/{slug}-{episode}", referer=f"{BASE_URL}/anime/{slug}")
    if not r:
        return []

    streams = []
    seen_urls = set()

    # Find `var videos = [["Server1", "url1", 0, 0], ...]`
    m = re.search(r'var\s+videos\s*=\s*(\[\[.*?\]\]);', r.text, re.DOTALL)
    if m:
        try:
            # Unescape JSON
            raw = m.group(1).replace("\\/", "/").replace("\\\"", "\"")
            videos = json.loads(raw)
            for v in videos:
                if len(v) >= 2:
                    name = v[0]
                    embed_url = v[1]
                    if not embed_url or embed_url in seen_urls:
                        continue
                    seen_urls.add(embed_url)
                    try:
                        playable = extract_stream(embed_url, name.lower())
                    except Exception as e:
                        log.warning(f"extract_stream failed for {embed_url}: {e}")
                        playable = None
                    if playable:
                        streams.append({
                            "name": f"StreamFlix {name}",
                            "title": f"TioAnime • {name}\n{slug} EP{episode}",
                            "url": playable,
                        })
        except (json.JSONDecodeError, IndexError) as e:
            log.warning(f"Failed to parse TioAnime videos array: {e}")

    return streams
