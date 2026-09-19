"""
Auto-scraper: when Stremio asks for a movie or episode by IMDb ID,
this module searches across ALL providers in parallel and aggregates
the best streams.

Strategy:
  1. Convert IMDb ID → TMDB ID (using TMDB API if available)
  2. Search FlixLatam: try /vidurl/{imdb_id}/ directly (works for movies)
  3. Search Fanpelis: query API for the title (need TMDB metadata)
  4. Search SeriesFlix: query web search for the title
  5. Rank streams: 1080p > 720p > SD, Latino > Castellano > Sub

Cache: resolved streams are cached for 1 hour per (item_type, item_id).
If Stremio asks for the same item again within the cache window, we return
the cached result instantly. Use invalidate_cache() to force a refresh.
"""

import os
import re
import time
import logging
import concurrent.futures
from typing import Optional

import requests

log = logging.getLogger(__name__)

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"

# ============================================================
# Stream cache (in-memory, 1 hour TTL)
# ============================================================
_STREAM_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 3600  # 1 hour in seconds


def _cache_key(item_type: str, item_id: str) -> str:
    """Build a cache key from item type and ID (strips .json suffix)."""
    clean_id = item_id.replace(".json", "")
    return f"{item_type}:{clean_id}"


def get_cached_streams(item_type: str, item_id: str) -> Optional[list[dict]]:
    """Return cached streams if they exist and are not expired, else None."""
    key = _cache_key(item_type, item_id)
    if key in _STREAM_CACHE:
        ts, streams = _STREAM_CACHE[key]
        if time.time() - ts < _CACHE_TTL:
            log.info(f"Cache HIT for {key} (age={int(time.time()-ts)}s)")
            return streams
        else:
            log.info(f"Cache EXPIRED for {key}")
            del _STREAM_CACHE[key]
    return None


def set_cached_streams(item_type: str, item_id: str, streams: list[dict]) -> None:
    """Store streams in the cache with current timestamp."""
    key = _cache_key(item_type, item_id)
    _STREAM_CACHE[key] = (time.time(), streams)
    log.info(f"Cache SET for {key} ({len(streams)} streams)")


def invalidate_cache(item_type: str, item_id: str) -> bool:
    """Remove a specific item from the cache. Returns True if it was present."""
    key = _cache_key(item_type, item_id)
    if key in _STREAM_CACHE:
        del _STREAM_CACHE[key]
        log.info(f"Cache INVALIDATED for {key}")
        return True
    return False


def get_cache_stats() -> dict:
    """Return cache statistics (size, oldest entry age, etc.)."""
    now = time.time()
    items = []
    for key, (ts, streams) in _STREAM_CACHE.items():
        items.append({
            "key": key,
            "age_seconds": int(now - ts),
            "stream_count": len(streams),
        })
    return {
        "total_items": len(_STREAM_CACHE),
        "ttl_seconds": _CACHE_TTL,
        "items": sorted(items, key=lambda x: x["age_seconds"]),
    }


def _tmdb_find(imdb_id: str, is_movie: bool) -> Optional[dict]:
    """Use TMDB /find to convert IMDb ID → TMDB metadata."""
    if not TMDB_API_KEY:
        return None
    try:
        r = requests.get(
            f"{TMDB_BASE}/find/{imdb_id}",
            params={"api_key": TMDB_API_KEY, "external_source": "imdb_id", "language": "es-ES"},
            timeout=6,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if is_movie and data.get("movie_results"):
            m = data["movie_results"][0]
            return {
                "tmdb_id": m.get("id"),
                "title": m.get("title", ""),
                "overview": m.get("overview", ""),
                "poster": f"https://image.tmdb.org/t/p/w500{m.get('poster_path')}" if m.get("poster_path") else None,
                "year": (m.get("release_date") or "")[:4],
                "rating": m.get("vote_average"),
                "genres": [],  # would need separate call
            }
        if not is_movie and data.get("tv_results"):
            t = data["tv_results"][0]
            return {
                "tmdb_id": t.get("id"),
                "title": t.get("name", ""),
                "overview": t.get("overview", ""),
                "poster": f"https://image.tmdb.org/t/p/w500{t.get('poster_path')}" if t.get("poster_path") else None,
                "year": (t.get("first_air_date") or "")[:4],
                "rating": t.get("vote_average"),
                "genres": [],
            }
    except Exception as e:
        log.warning(f"TMDB find failed for {imdb_id}: {e}")
    return None


# ============================================================
# Quality ranking
# ============================================================
def _quality_score(stream: dict) -> int:
    """Higher = better. Used to sort streams."""
    name = stream.get("name", "").lower()
    title = stream.get("title", "").lower()
    text = name + " " + title
    score = 0
    # Quality
    if "1080" in text: score += 100
    elif "720" in text: score += 50
    elif "hd" in text: score += 30
    elif "4k" in text or "2160" in text: score += 200
    # Language preference (latino > castellano > sub)
    if "lat" in text: score += 20
    elif "cast" in text or "esp" in text: score += 15
    elif "sub" in text: score += 5
    # Dual audio bonus
    if "dual" in text: score += 10
    return score


def sort_streams(streams: list[dict]) -> list[dict]:
    """Sort streams by quality (best first)."""
    return sorted(streams, key=_quality_score, reverse=True)


# ============================================================
# Provider search functions
# ============================================================
def _try_flixlatam_movie(imdb_id: str) -> list[dict]:
    """Try FlixLatam with the IMDb ID directly."""
    try:
        from providers import flixlatam
        return flixlatam.resolve_movie_streams(imdb_id)
    except Exception as e:
        log.warning(f"FlixLatam movie search failed: {e}")
        return []


def _try_flixlatam_episode(imdb_id: str, season: int, episode: int) -> list[dict]:
    """Try FlixLatam episode with the IMDb ID directly."""
    try:
        from providers import flixlatam
        return flixlatam.resolve_episode_streams(imdb_id, season, episode)
    except Exception as e:
        log.warning(f"FlixLatam episode search failed: {e}")
        return []


def _try_fanpelis_movie(title: str) -> list[dict]:
    """Search Fanpelis by title."""
    try:
        from providers import fanpelis
        results = fanpelis.search(title, item_type="movies")
        if not results:
            return []
        # Take the first match
        first = results[0]
        post_id = first.get("post_id")
        if post_id:
            return fanpelis.resolve_streams(post_id)
    except Exception as e:
        log.warning(f"Fanpelis movie search failed: {e}")
    return []


def _try_fanpelis_series(title: str, season: int, episode: int) -> list[dict]:
    """Search Fanpelis series by title."""
    try:
        from providers import fanpelis
        results = fanpelis.search(title, item_type="tvshows")
        if not results:
            return []
        first = results[0]
        slug = first.get("slug")
        if not slug:
            return []
        # Get episodes
        info = fanpelis.get_tvshow_detail(slug)
        if not info or not info.get("post_id"):
            return []
        episodes = fanpelis.get_episodes(info["post_id"])
        # Find the matching episode
        for ep in episodes:
            if ep.get("season") == season and ep.get("episode") == episode:
                ep_id = ep.get("_id") or ep.get("id")
                if ep_id:
                    return fanpelis.resolve_streams(ep_id)
    except Exception as e:
        log.warning(f"Fanpelis series search failed: {e}")
    return []


def _try_seriesflix_series(title: str, season: int, episode: int) -> list[dict]:
    """Search SeriesFlix by title."""
    try:
        from providers import seriesflix
        results = seriesflix.search_series(title)
        if not results:
            return []
        first = results[0]
        slug = first.get("slug")
        if not slug:
            return []
        return seriesflix.resolve_episode_streams(slug, season, episode)
    except Exception as e:
        log.warning(f"SeriesFlix search failed: {e}")
    return []


def _try_tioanime(title: str, episode: int) -> list[dict]:
    """Search TioAnime by title."""
    try:
        from providers import tioanime
        results = tioanime.search_anime(title)
        if not results:
            return []
        first = results[0]
        slug = first.get("slug")
        if not slug:
            return []
        return tioanime.get_episode_streams(slug, episode)
    except Exception as e:
        log.warning(f"TioAnime search failed: {e}")
    return []


def _try_latanime(title: str, episode: int) -> list[dict]:
    """Search Latanime by title."""
    try:
        from providers import latanime
        results = latanime.search_anime(title)
        if not results:
            return []
        first = results[0]
        slug = first.get("slug")
        if not slug:
            return []
        return latanime.get_episode_streams(slug, episode)
    except Exception as e:
        log.warning(f"Latanime search failed: {e}")
    return []


# ============================================================
# Aggregator
# ============================================================
def find_movie_streams(imdb_id: str) -> list[dict]:
    """
    Find streams for a movie given an IMDb ID.
    
    Tries all providers in parallel:
      1. FlixLatam (direct IMDb ID lookup - fastest)
      2. Fanpelis (search by title via TMDB metadata)
    
    Results are cached for 1 hour. Use invalidate_cache() to force refresh.
    """
    # Check cache first
    cached = get_cached_streams("movie", imdb_id)
    if cached is not None:
        return cached

    # Get TMDB metadata for title-based searches
    tmdb_info = _tmdb_find(imdb_id, is_movie=True)
    title = tmdb_info.get("title") if tmdb_info else ""

    all_streams = []

    # Run searches in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_try_flixlatam_movie, imdb_id): "FlixLatam",
        }
        if title:
            futures[executor.submit(_try_fanpelis_movie, title)] = "Fanpelis"

        for future in concurrent.futures.as_completed(futures, timeout=60):
            provider = futures[future]
            try:
                streams = future.result(timeout=60)
                for s in streams:
                    # Tag with provider if not already
                    if provider not in s.get("name", ""):
                        s["name"] = f"{s['name']} ({provider})"
                all_streams.extend(streams)
                log.info(f"Auto-scrape {provider}: {len(streams)} streams")
            except Exception as e:
                log.warning(f"Auto-scrape {provider} error: {e}")

    # Deduplicate by URL
    seen_urls = set()
    deduped = []
    for s in all_streams:
        url = s.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(s)

    result = sort_streams(deduped)
    # Cache the result
    set_cached_streams("movie", imdb_id, result)
    return result


def find_episode_streams(imdb_id: str, season: int, episode: int) -> list[dict]:
    """
    Find streams for an episode given an IMDb ID.
    
    Tries all providers in parallel:
      1. FlixLatam (direct IMDb ID lookup)
      2. SeriesFlix (search by title)
      3. Fanpelis (search by title)
    
    Results are cached for 1 hour. Use invalidate_cache() to force refresh.
    """
    # Cache key includes season and episode
    cache_id = f"{imdb_id}:{season}:{episode}"
    cached = get_cached_streams("series", cache_id)
    if cached is not None:
        return cached

    tmdb_info = _tmdb_find(imdb_id, is_movie=False)
    title = tmdb_info.get("title") if tmdb_info else ""

    all_streams = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_try_flixlatam_episode, imdb_id, season, episode): "FlixLatam",
        }
        if title:
            futures[executor.submit(_try_seriesflix_series, title, season, episode)] = "SeriesFlix"
            futures[executor.submit(_try_fanpelis_series, title, season, episode)] = "Fanpelis"

        for future in concurrent.futures.as_completed(futures, timeout=90):
            provider = futures[future]
            try:
                streams = future.result(timeout=90)
                for s in streams:
                    if provider not in s.get("name", ""):
                        s["name"] = f"{s['name']} ({provider})"
                all_streams.extend(streams)
                log.info(f"Auto-scrape {provider}: {len(streams)} streams")
            except Exception as e:
                log.warning(f"Auto-scrape {provider} error: {e}")

    seen_urls = set()
    deduped = []
    for s in all_streams:
        url = s.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(s)

    result = sort_streams(deduped)
    set_cached_streams("series", cache_id, result)
    return result


def find_anime_streams(title: str, episode: int) -> list[dict]:
    """
    Find streams for an anime episode given a title.
    Searches both TioAnime and Latanime.
    """
    all_streams = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(_try_tioanime, title, episode): "TioAnime",
            executor.submit(_try_latanime, title, episode): "Latanime",
        }
        for future in concurrent.futures.as_completed(futures, timeout=60):
            provider = futures[future]
            try:
                streams = future.result(timeout=60)
                for s in streams:
                    if provider not in s.get("name", ""):
                        s["name"] = f"{s['name']} ({provider})"
                all_streams.extend(streams)
                log.info(f"Anime search {provider}: {len(streams)} streams")
            except Exception as e:
                log.warning(f"Anime search {provider} error: {e}")

    seen_urls = set()
    deduped = []
    for s in all_streams:
        url = s.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(s)

    return sort_streams(deduped)
