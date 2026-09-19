"""
SeriesFlix provider: scrapes seriesflixhd.lol (catalog) and seriesflixhd.team (episodes)
for series in Spanish/Latino/Subtitulado.

URL structure:
  Catalog:  https://seriesflixhd.lol/series?page=N         (listing)
            https://seriesflixhd.lol/?s=QUERY              (search)
  Detail:   https://seriesflixhd.lol/serie/{slug}          (series detail + season list)
  Seasons:  https://seriesflixhd.team/temporada/{slug}-{N}/   (season page with episode list)
  Episodes: https://seriesflixhd.team/episodio/{slug}-{S}x{E}/  (episode with embeds)

Episode pages contain `<div data-url="base64-encoded-nupload-url">` elements
pointing to nupload.my. There are usually 3 servers: LATINO HD, CASTELLANO HD,
SUBTITULADO HD.

The nupload.my page contains obfuscated JS that builds the HLS URL using:
  1. An array of base64-encoded strings
  2. Each string is decoded (base64), digits extracted, minus a magic number, converted to ASCII char
  3. All chars concatenated form the base URL
  4. Append "?s=" + sesz (session token) to get the playable HLS m3u8 URL
"""

import re
import base64
import logging
import urllib3
from typing import Optional
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

from extractors.hosts import extract_stream

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

# Catalog lives on .lol, episodes on .team
CATALOG_URL = "https://seriesflixhd.lol"
EPISODES_URL = "https://seriesflixhd.team"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"}


def _get(url: str, referer: Optional[str] = None) -> Optional[requests.Response]:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=15, verify=False)
        if r.status_code != 200:
            log.debug(f"GET {url} -> {r.status_code}")
            return None
        return r
    except Exception as e:
        log.warning(f"GET {url} failed: {e}")
        return None


# ============================================================
# Catalog scraping (seriesflixhd.lol)
# ============================================================
def _parse_series_card(card) -> Optional[dict]:
    href = card.get("href")
    if not href:
        return None
    href = urljoin(CATALOG_URL, href)
    if "/serie/" not in href:
        return None
    m = re.search(r"/serie/([^/?#]+)", href)
    if not m:
        return None
    slug = m.group(1)
    img = card.find("img")
    poster = None
    if img:
        poster = img.get("src") or img.get("data-src") or ""
        if poster and not poster.startswith("http"):
            poster = urljoin(CATALOG_URL, poster)
    title = ""
    if img:
        title = img.get("alt", "").strip()
    if not title:
        title_el = card.find(class_=re.compile("title|name", re.I))
        if title_el:
            title = title_el.text.strip()
    if not title:
        title = slug.replace("-", " ").title()
    return {
        "id": f"seriesflix:{slug}",
        "type": "series",
        "name": title,
        "poster": poster or None,
        "slug": slug,
    }


def get_series_list(page: int = 1, query: str = "") -> list[dict]:
    """Get a page of series from the catalog."""
    if query:
        path = f"/?s={quote(query)}"
    else:
        # seriesflixhd.lol uses /series-online for the listing
        path = f"/series-online/page/{page}/" if page > 1 else "/series-online"
    r = _get(CATALOG_URL + path)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    seen = set()
    out = []
    for a in soup.select("a[href*='/serie/']"):
        if a.find("img"):
            item = _parse_series_card(a)
            if item and item["slug"] not in seen:
                seen.add(item["slug"])
                out.append(item)
    return out


def search_series(query: str) -> list[dict]:
    return get_series_list(page=1, query=query)


# ============================================================
# Detail page (seriesflixhd.lol/serie/{slug})
# ============================================================
def get_series_detail(slug: str) -> Optional[dict]:
    """
    Get series detail: title, poster, description, seasons (list of season numbers).

    Episode list is not fetched here (would require loading each season page).
    Episodes are resolved on-demand in resolve_episode_streams().
    """
    r = _get(f"{CATALOG_URL}/serie/{slug}")
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    info = {"slug": slug, "type": "series", "seasons": []}

    title = soup.find("title")
    if title:
        info["name"] = re.sub(r"\s*Online.*$", "", title.text, flags=re.I).strip()
    else:
        info["name"] = slug.replace("-", " ").title()

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if any(k in src.lower() for k in ["poster", "portada", "imagen", "cover"]):
            if not src.startswith("http"):
                src = urljoin(CATALOG_URL, src)
            info["poster"] = src
            break

    desc = soup.find("p", class_=re.compile("sinop|description|storyline|text"))
    if desc:
        info["description"] = desc.text.strip()

    # Find season links: /temporada/{slug}-{N}/
    ep_pattern = re.compile(rf"/temporada/{re.escape(slug)}-(\d+)/")
    seasons = set()
    for m in ep_pattern.finditer(r.text):
        seasons.add(int(m.group(1)))
    info["seasons"] = sorted(seasons)
    return info


def get_season_episodes(slug: str, season: int) -> list[int]:
    """Get a list of episode numbers for a season."""
    r = _get(f"{EPISODES_URL}/temporada/{slug}-{season}/")
    if not r:
        return []
    # Pattern: /episodio/{slug}-{season}x{episode}/
    ep_pattern = re.compile(rf"/episodio/{re.escape(slug)}-{season}x(\d+)/")
    episodes = set()
    for m in ep_pattern.finditer(r.text):
        episodes.add(int(m.group(1)))
    return sorted(episodes)


# ============================================================
# Episode stream resolution (seriesflixhd.team + nupload.my)
# ============================================================
def _extract_nupload_stream(html: str) -> Optional[str]:
    """
    Extract the playable HLS URL from a nupload.my page.

    The page has:
      var ARRAYNAME = ["base64_1", "base64_2", ...];
      var URLVAR = ""; ARRAYNAME.forEach(function(value) {
        URLVAR += String.fromCharCode(parseInt(atob(value).replace(/\\D/g,'')) - NUMBER);
      });
      var sesz = "session_token";
      player.setup({file: URLVAR + "?s=" + sesz, type: "hls", ...});
    """
    # 1. Find the forEach pattern (gives us array name and subtract number)
    m_foreach = re.search(
        r'(\w+)\.forEach\(function\s+\w+\(\w+\)\s*\{\s*(\w+)\s*\+=\s*String\.fromCharCode\(parseInt\(atob\(\w+\)\.replace\([^)]+\)\)\s*-\s*(\d+)\)',
        html,
    )
    if not m_foreach:
        return None
    array_name = m_foreach.group(1)
    subtract = int(m_foreach.group(3))

    # 2. Find the array definition
    pattern = re.compile(r'var\s+' + re.escape(array_name) + r'\s*=\s*\[([^\]]+)\]', re.DOTALL)
    m_array = pattern.search(html)
    if not m_array:
        return None
    array_str = m_array.group(1)
    items = re.findall(r'"([^"]+)"', array_str)
    if not items:
        items = re.findall(r"'([^']+)'", array_str)

    # 3. Decode each item
    url = ""
    for item in items:
        try:
            decoded = base64.b64decode(item).decode("utf-8", errors="replace")
            digits = re.sub(r"\D", "", decoded)
            if digits:
                num = int(digits) - subtract
                if 0 <= num <= 0x10FFFF:
                    url += chr(num)
        except Exception:
            continue

    # 4. Find sesz
    sesz_match = re.search(r'var\s+sesz\s*=\s*"([^"]+)"', html)
    sesz = sesz_match.group(1) if sesz_match else ""

    if not url:
        return None
    return f"{url}?s={sesz}"


def resolve_episode_streams(slug: str, season: int, episode: int) -> list[dict]:
    """
    Get all streams for a specific episode.

    Returns:
        [{"name": "StreamFlix [LAT]", "title": ..., "url": playable_url}]
    """
    ep_url = f"{EPISODES_URL}/episodio/{slug}-{season}x{episode}/"
    r = _get(ep_url, referer=f"{EPISODES_URL}/temporada/{slug}-{season}/")
    if not r:
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    # Find all <div data-url="base64-encoded-URL"> servers
    streams = []
    seen_urls = set()
    for el in soup.find_all(attrs={"data-url": True}):
        enc = el.get("data-url", "")
        label = el.text.strip()
        if not enc:
            continue
        try:
            embed_url = base64.b64decode(enc).decode("utf-8").strip()
        except Exception:
            continue
        if not embed_url or embed_url in seen_urls:
            continue
        seen_urls.add(embed_url)

        # Detect language from label
        lang = "LAT"  # default
        if "CASTELLANO" in label.upper():
            lang = "CAST"
        elif "SUBTITULADO" in label.upper() or "SUB" in label.upper():
            lang = "SUB"

        # Detect quality
        quality = "HD"
        if "1080" in label:
            quality = "1080p"
        elif "720" in label:
            quality = "720p"

        # Resolve the nupload URL
        try:
            r2 = _get(embed_url, referer=EPISODES_URL)
            if not r2:
                continue
            playable = _extract_nupload_stream(r2.text)
        except Exception as e:
            log.warning(f"nupload resolve failed for {embed_url}: {e}")
            playable = None

        if playable:
            streams.append({
                "name": f"StreamFlix [{lang}] {quality}",
                "title": f"SeriesFlix • {lang} {quality}\nEl Mentalista T{season}E{episode}",
                "url": playable,
            })

    return streams
