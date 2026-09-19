"""
StreamFlix Stremio Addon — main app.

Exposes Stremio addon endpoints:
    GET /manifest.json
    GET /catalog/:type/:id.json               (catalog browsing)
    GET /catalog/:type/:id/search=:query.json (search)
    GET /meta/:type/:id.json                  (metadata)
    GET /stream/:type/:id.json                (stream resolution)

Plus a simple HTML UI at /ui for manual testing (no Gradio, to keep startup fast).

Stremio ID conventions:
    Movies:  flixlatam:{slug}      e.g. flixlatam:sultana-hUFIlu
    Series:  flixlatam:{slug}      e.g. flixlatam:en-coma-BVwton
    Episodes: flixlatam:{slug}:1:1 (slug:season:episode)

When Stremio asks for /stream/movie/tt0816692.json (raw IMDb ID), we look up
the slug by trying the IMDb ID directly against flixlatam's /vidurl/{imdb}/
endpoint. If that works, great. If not, we return [].
"""

import os
import re
import logging
from typing import Optional

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
import uvicorn

# Local imports - make sure 'providers' and 'utils' are importable
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from providers import flixlatam
from providers import latanime
from providers import seriesflix
from providers import tioanime
from providers import fanpelis
from utils.flaresolverr import is_enabled as flaresolverr_enabled

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("streamflix")

# ============================================================
# Config
# ============================================================
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
ADDON_NAME = os.environ.get("ADDON_NAME", "StreamFlix Reborn (FlixLatam)")
ADDON_VERSION = "1.0.0"
ADDON_ID = "community.streamflix.flixlatam"

# ============================================================
# FastAPI app
# ============================================================
app = FastAPI(title="StreamFlix Stremio Addon")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Stremio addon: manifest
# ============================================================
@app.get("/manifest.json")
def manifest():
    catalogs = [
        {
            "type": "movie",
            "id": "flixlatam_movies",
            "name": "FlixLatam · Películas",
            "extraSupported": ["search", "page"],
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "page", "isRequired": False, "options": [1, 2, 3, 4, 5]},
            ],
        },
        {
            "type": "movie",
            "id": "flixlatam_popular",
            "name": "FlixLatam · Populares",
            "extraSupported": ["page"],
        },
        {
            "type": "series",
            "id": "flixlatam_series",
            "name": "FlixLatam · Series",
            "extraSupported": ["search", "page"],
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "page", "isRequired": False, "options": [1, 2, 3, 4, 5]},
            ],
        },
        {
            "type": "series",
            "id": "latanime_catalog",
            "name": "Latanime · Anime",
            "extraSupported": ["search", "page"],
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "page", "isRequired": False, "options": [1, 2, 3, 4, 5]},
            ],
        },
        {
            "type": "series",
            "id": "seriesflix_catalog",
            "name": "SeriesFlix · Series HD",
            "extraSupported": ["search", "page"],
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "page", "isRequired": False, "options": [1, 2, 3, 4, 5]},
            ],
        },
        {
            "type": "series",
            "id": "tioanime_catalog",
            "name": "TioAnime · Anime",
            "extraSupported": ["search", "page"],
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "page", "isRequired": False, "options": [1, 2, 3, 4, 5]},
            ],
        },
        {
            "type": "movie",
            "id": "fanpelis_movies",
            "name": "Fanpelis · Películas HD",
            "extraSupported": ["search", "page"],
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "page", "isRequired": False, "options": [1, 2, 3, 4, 5]},
            ],
        },
        {
            "type": "series",
            "id": "fanpelis_series",
            "name": "Fanpelis · Series HD",
            "extraSupported": ["search", "page"],
            "extra": [
                {"name": "search", "isRequired": False},
                {"name": "page", "isRequired": False, "options": [1, 2, 3, 4, 5]},
            ],
        },
    ]
    return {
        "id": ADDON_ID,
        "version": ADDON_VERSION,
        "name": ADDON_NAME,
        "description": "Streaming en Español y Latino desde FlixLatam + Latanime + SeriesFlix + TioAnime + Fanpelis. "
                       "Catálogos completos + resolutor de streams.",
        "logo": "https://flixlatam.com/images/logo.png",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "flixlatam:", "latanime:", "seriesflix:", "tioanime:", "fanpelis:"],
        "catalogs": catalogs,
        "behaviorHints": {"configurable": False},
    }


# ============================================================
# Stremio addon: catalog
# ============================================================
@app.get("/catalog/{item_type}/{cat_id}.json")
def catalog(item_type: str, cat_id: str, request: Request):
    page = int(request.query_params.get("page", 1) or 1)
    search = request.query_params.get("search", "").strip()

    log.info(f"catalog type={item_type} cat={cat_id} page={page} search={search!r}")

    items = []
    try:
        if cat_id == "flixlatam_movies":
            if search:
                items = _search_flixlatam(search, "movie")
            else:
                items = flixlatam.get_movies(page=page)
        elif cat_id == "flixlatam_popular":
            items = flixlatam.get_popular_movies(page=page)
        elif cat_id == "flixlatam_series":
            if search:
                items = _search_flixlatam(search, "series")
            else:
                items = flixlatam.get_series(page=page)
        elif cat_id == "latanime_catalog":
            if search:
                items = latanime.search_anime(search)
            else:
                items = latanime.get_anime_list(page=page)
        elif cat_id == "seriesflix_catalog":
            if search:
                items = seriesflix.search_series(search)
            else:
                items = seriesflix.get_series_list(page=page)
        elif cat_id == "tioanime_catalog":
            if search:
                items = tioanime.search_anime(search)
            else:
                items = tioanime.get_anime_list(page=page)
        elif cat_id == "fanpelis_movies":
            items = fanpelis.get_movies(page=page, query=search)
        elif cat_id == "fanpelis_series":
            items = fanpelis.get_tvshows(page=page, query=search)
    except Exception as e:
        log.exception(f"catalog error: {e}")

    # Convert to Stremio metas
    metas = []
    for it in items:
        meta = {
            "id": it["id"] if "id" in it and it["id"].startswith(("flixlatam:", "latanime:")) 
                  else f"{cat_id.split('_')[0]}:{it['slug']}",
            "type": it["type"],
            "name": it["name"],
            "poster": it.get("poster"),
        }
        metas.append(meta)

    return {"metas": metas}


def _search_flixlatam(query: str, item_type: str) -> list[dict]:
    """
    Search FlixLatam. Their site uses JS-based search that returns no
    results on the server side, so we fall back to returning the first
    page of the catalog. Stremio will then use the IMDb IDs to cross-
    reference with TMDB/Cinemeta (its own search) and find the right item.
    """
    # Try the URL pattern that flixlatam uses for query strings (no-op
    # filter, returns full catalog) — at least gives Stremio something
    # to display.
    if item_type == "movie":
        return flixlatam.get_movies(page=1)
    return flixlatam.get_series(page=1)


# ============================================================
# Stremio addon: meta
# ============================================================
@app.get("/meta/{item_type}/{item_id}.json")
def meta(item_type: str, item_id: str):
    log.info(f"meta type={item_type} id={item_id}")

    # item_id can be:
    #   flixlatam:{slug}    -> FlixLatam movie/series
    #   latanime:{slug}     -> Latanime series
    #   tt{digits}          -> IMDb ID (we have to find the slug)
    if item_id.startswith("flixlatam:"):
        slug = item_id[len("flixlatam:"):]
        if item_type == "movie":
            info = flixlatam.get_movie_detail(slug)
        else:
            info = flixlatam.get_series_detail(slug)
        if not info:
            return JSONResponse(status_code=404, content={"error": "not found"})
        m = {
            "id": f"flixlatam:{slug}",
            "type": item_type,
            "name": info.get("name", slug),
            "poster": info.get("poster"),
            "description": info.get("description", ""),
            "genres": info.get("genres", []),
        }
        if item_type == "series":
            videos = []
            for season, episodes in info.get("seasons", {}).items():
                for ep in episodes:
                    videos.append({
                        "id": f"flixlatam:{slug}:{season}:{ep}",
                        "title": f"T{season}E{ep}",
                        "season": season,
                        "episode": ep,
                    })
            m["videos"] = videos
        return {"meta": m}

    elif item_id.startswith("latanime:"):
        slug = item_id[len("latanime:"):]
        info = latanime.get_anime_detail(slug)
        if not info:
            return JSONResponse(status_code=404, content={"error": "not found"})
        m = {
            "id": f"latanime:{slug}",
            "type": "series",
            "name": info.get("name", slug),
            "poster": info.get("poster"),
            "description": info.get("description", ""),
            "genres": info.get("genres", []),
        }
        videos = []
        for ep in info.get("episodes", []):
            videos.append({
                "id": f"latanime:{slug}:{ep['season']}:{ep['episode']}",
                "title": f"EP{ep['episode']}",
                "season": ep["season"],
                "episode": ep["episode"],
            })
        m["videos"] = videos
        return {"meta": m}

    elif item_id.startswith("seriesflix:"):
        slug = item_id[len("seriesflix:"):]
        info = seriesflix.get_series_detail(slug)
        if not info:
            return JSONResponse(status_code=404, content={"error": "not found"})
        m = {
            "id": f"seriesflix:{slug}",
            "type": "series",
            "name": info.get("name", slug),
            "poster": info.get("poster"),
            "description": info.get("description", ""),
        }
        # Build videos list from seasons (fetch episodes for each)
        videos = []
        for season in info.get("seasons", []):
            try:
                episodes = seriesflix.get_season_episodes(slug, season)
            except Exception as e:
                log.warning(f"get_season_episodes failed: {e}")
                episodes = []
            for ep in episodes:
                videos.append({
                    "id": f"seriesflix:{slug}:{season}:{ep}",
                    "title": f"T{season}E{ep}",
                    "season": season,
                    "episode": ep,
                })
        m["videos"] = videos
        return {"meta": m}

    elif item_id.startswith("tioanime:"):
        slug = item_id[len("tioanime:"):]
        info = tioanime.get_anime_detail(slug)
        if not info:
            return JSONResponse(status_code=404, content={"error": "not found"})
        m = {
            "id": f"tioanime:{slug}",
            "type": "series",
            "name": info.get("name", slug),
            "poster": info.get("poster"),
            "description": info.get("description", ""),
            "genres": info.get("genres", []),
        }
        videos = []
        for ep in info.get("episodes", []):
            videos.append({
                "id": f"tioanime:{slug}:{ep['season']}:{ep['episode']}",
                "title": f"EP{ep['episode']}",
                "season": ep["season"],
                "episode": ep["episode"],
            })
        m["videos"] = videos
        return {"meta": m}

    elif item_id.startswith("fanpelis:"):
        slug = item_id[len("fanpelis:"):]
        # Detect if it's a movie or series
        if item_type == "movie":
            info = fanpelis.get_movie_detail(slug)
            if not info:
                return JSONResponse(status_code=404, content={"error": "not found"})
            return {"meta": {
                "id": f"fanpelis:{slug}",
                "type": "movie",
                "name": info.get("name", slug),
                "poster": info.get("poster"),
                "description": info.get("description", ""),
            }}
        else:
            info = fanpelis.get_tvshow_detail(slug)
            if not info:
                return JSONResponse(status_code=404, content={"error": "not found"})
            m = {
                "id": f"fanpelis:{slug}",
                "type": "series",
                "name": info.get("name", slug),
                "poster": info.get("poster"),
                "description": info.get("description", ""),
            }
            # Fetch episodes (we have post_id)
            post_id = info.get("post_id")
            videos = []
            if post_id:
                try:
                    eps = fanpelis.get_episodes(post_id)
                    for ep in eps:
                        # Each episode has season/episode info
                        season = ep.get("season", 1)
                        episode_num = ep.get("episode", 1)
                        ep_id = ep.get("_id") or ep.get("id")
                        videos.append({
                            "id": f"fanpelis:{slug}:{season}:{episode_num}:{ep_id}",
                            "title": f"T{season}E{episode_num}",
                            "season": season,
                            "episode": episode_num,
                        })
                except Exception as e:
                    log.warning(f"get_episodes failed: {e}")
            m["videos"] = videos
            return {"meta": m}

    elif item_id.startswith("tt"):
        return _minimal_imdb_meta(item_type, item_id)

    else:
        return JSONResponse(
            status_code=404,
            content={"error": f"Unknown id format: {item_id}"},
        )


def _minimal_imdb_meta(item_type: str, imdb_id: str) -> dict:
    """Build a minimal meta entry using just an IMDb ID (for /stream/ requests)."""
    name = imdb_id  # Stremio will fall back to TMDB/Cinemeta for the real name
    return {
        "meta": {
            "id": imdb_id,
            "type": item_type,
            "name": name,
        }
    }


# ============================================================
# Stremio addon: stream
# ============================================================
@app.get("/stream/{item_type}/{item_id}.json")
def stream(item_type: str, item_id: str):
    log.info(f"stream type={item_type} id={item_id}")

    # Strip .json if present
    item_id = item_id.replace(".json", "")

    streams = []

    # FlixLatam items: flixlatam:{slug} or flixlatam:{slug}:{s}:{e}
    if "flixlatam:" in item_id:
        parts = item_id.split(":")
        # parts[0] == "flixlatam", parts[1] == slug, optional parts[2]=season, parts[3]=episode
        if len(parts) == 2 and item_type == "movie":
            # flixlatam:{slug} movie
            slug = parts[1]
            info = flixlatam.get_movie_detail(slug)
            if info and info.get("imdb_id"):
                streams = flixlatam.resolve_movie_streams(info["imdb_id"])
        elif len(parts) == 4 and item_type == "series":
            # flixlatam:{slug}:{season}:{episode}
            slug = parts[1]
            season = int(parts[2])
            episode = int(parts[3])
            ep_imdb = flixlatam.get_episode_imdb_id(slug, season, episode)
            if ep_imdb:
                m = re.match(r"(tt\d+)", ep_imdb)
                if m:
                    streams = flixlatam.resolve_episode_streams(m.group(1), season, episode)

    # Latanime items: latanime:{slug}:{season}:{episode}
    elif "latanime:" in item_id and item_type == "series":
        parts = item_id.split(":")
        if len(parts) >= 4:
            slug = parts[1]
            episode = int(parts[3])
            streams = latanime.get_episode_streams(slug, episode)

    # SeriesFlix items: seriesflix:{slug}:{season}:{episode}
    elif "seriesflix:" in item_id and item_type == "series":
        parts = item_id.split(":")
        if len(parts) >= 4:
            slug = parts[1]
            season = int(parts[2])
            episode = int(parts[3])
            streams = seriesflix.resolve_episode_streams(slug, season, episode)

    # TioAnime items: tioanime:{slug}:{season}:{episode}
    elif "tioanime:" in item_id and item_type == "series":
        parts = item_id.split(":")
        if len(parts) >= 4:
            slug = parts[1]
            episode = int(parts[3])
            streams = tioanime.get_episode_streams(slug, episode)

    # Fanpelis items: fanpelis:{slug} (movie) or fanpelis:{slug}:{s}:{e}:{post_id} (episode)
    elif "fanpelis:" in item_id:
        parts = item_id.split(":")
        slug = parts[1] if len(parts) > 1 else ""
        if item_type == "movie":
            info = fanpelis.get_movie_detail(slug)
            if info and info.get("post_id"):
                streams = fanpelis.resolve_streams(info["post_id"])
        elif item_type == "series" and len(parts) >= 5:
            # fanpelis:{slug}:{season}:{episode}:{post_id}
            post_id = int(parts[4]) if parts[4].isdigit() else None
            if post_id:
                streams = fanpelis.resolve_streams(post_id)

    # Plain IMDb IDs
    elif item_id.startswith("tt"):
        if item_type == "movie":
            streams = flixlatam.resolve_movie_streams(item_id)
        elif item_type == "series" and ":" in item_id:
            parts = item_id.split(":")
            if len(parts) >= 3:
                imdb_id = parts[0]
                season = int(parts[1])
                episode = int(parts[2])
                streams = flixlatam.resolve_episode_streams(imdb_id, season, episode)

    if not streams:
        return {"streams": []}
    return {"streams": streams}


# ============================================================
# Health check
# ============================================================
@app.get("/")
def home():
    return {
        "status": "online",
        "addon": ADDON_NAME,
        "version": ADDON_VERSION,
        "manifest": "/manifest.json",
        "ui": "/ui",
        "flaresolverr_enabled": flaresolverr_enabled(),
        "tmdb_configured": bool(TMDB_API_KEY),
    }


# ============================================================
# Simple HTML UI for manual testing (no Gradio, lightweight)
# ============================================================
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>StreamFlix Addon</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;background:#0f0f0f;color:#eee;line-height:1.5}
h1{color:#10b981;border-bottom:1px solid #333;padding-bottom:.5rem}
h2{color:#3291ff;margin-top:2rem}
input,button,select{padding:.6rem;margin:.2rem;background:#1a1a1a;border:1px solid #333;color:#eee;border-radius:4px;font-size:14px}
button{background:#10b981;color:#000;cursor:pointer;font-weight:600}
button:hover{background:#0e9c72}
pre{background:#1a1a1a;padding:1rem;border-radius:4px;overflow-x:auto;border:1px solid #333;max-height:400px}
a{color:#3291ff}
code{background:#1a1a1a;padding:2px 6px;border-radius:3px;color:#10b981}
.status{padding:.5rem 1rem;background:#1a2a1a;border-left:3px solid #10b981;margin:1rem 0;border-radius:4px}
</style>
</head>
<body>
<h1>StreamFlix Reborn — Stremio Addon</h1>
<div class="status">
<strong>Addon URL para Stremio:</strong><br>
<code id="manifest-url"></code>
</div>

<h2>Resolver Película</h2>
<p>IMDb ID (ej: tt41228546) o slug FlixLatam (ej: sultana-hUFIlu)</p>
<input id="movie-input" placeholder="tt41228546" style="width:300px">
<button onclick="resolveMovie()">Resolver</button>
<pre id="movie-result">Resultado aparecerá aquí...</pre>

<h2>Resolver Episodio</h2>
<p>IMDb ID (ej: tt45403168) o slug Latanime (ej: la-mision-de-la-familia-yozakura-s2-castellano)</p>
<input id="ep-id" placeholder="tt45403168" style="width:300px">
<input id="ep-s" type="number" value="1" style="width:60px" placeholder="T">
<input id="ep-e" type="number" value="1" style="width:60px" placeholder="E">
<button onclick="resolveEpisode()">Resolver</button>
<pre id="ep-result">Resultado aparecerá aquí...</pre>

<h2>Explorar Catálogo</h2>
<select id="cat-type">
<option value="movie/flixlatam_movies">FlixLatam Películas</option>
<option value="movie/flixlatam_popular">FlixLatam Populares</option>
<option value="series/flixlatam_series">FlixLatam Series</option>
<option value="series/latanime_catalog">Latanime Anime</option>
</select>
<input id="cat-page" type="number" value="1" style="width:60px" placeholder="Pág">
<button onclick="browseCatalog()">Cargar</button>
<pre id="cat-result">Resultado aparecerá aquí...</pre>

<script>
const MANIFEST = location.origin + '/manifest.json';
document.getElementById('manifest-url').textContent = MANIFEST;

async function resolveMovie() {
  const v = document.getElementById('movie-input').value.trim();
  const r = await fetch(`/stream/movie/${v}.json`);
  const d = await r.json();
  document.getElementById('movie-result').textContent = JSON.stringify(d, null, 2);
}

async function resolveEpisode() {
  const id = document.getElementById('ep-id').value.trim();
  const s = document.getElementById('ep-s').value;
  const e = document.getElementById('ep-e').value;
  const r = await fetch(`/stream/series/${id}:${s}:${e}.json`);
  const d = await r.json();
  document.getElementById('ep-result').textContent = JSON.stringify(d, null, 2);
}

async function browseCatalog() {
  const t = document.getElementById('cat-type').value;
  const p = document.getElementById('cat-page').value;
  const r = await fetch(`/catalog/${t}.json?page=${p}`);
  const d = await r.json();
  document.getElementById('cat-result').textContent = JSON.stringify(d, null, 2).slice(0, 5000);
}
</script>
</body>
</html>"""


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    log.info(f"Starting StreamFlix Stremio addon on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
