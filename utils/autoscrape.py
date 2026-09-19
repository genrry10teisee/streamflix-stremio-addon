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
"""

import os
import re
import logging
import concurrent.futures
from typing import Optional

import requests

log = logging.getLogger(__name__)

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE = "https://api.themoviedb.org/3"


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
    """
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

    return sort_streams(deduped)


def find_episode_streams(imdb_id: str, season: int, episode: int) -> list[dict]:
    """
    Find streams for an episode given an IMDb ID.
    
    Tries all providers in parallel:
      1. FlixLatam (direct IMDb ID lookup)
      2. SeriesFlix (search by title)
      3. Fanpelis (search by title)
    """
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

    return sort_streams(deduped)


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
