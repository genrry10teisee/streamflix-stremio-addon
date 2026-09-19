"""
StreamFlix Stremio Addon — main app.

Exposes Stremio addon endpoints:
    GET /manifest.json
    GET /catalog/:type/:id.json               (catalog browsing)
    GET /catalog/:type/:id/search=:query.json (search)
    GET /meta/:type/:id.json                  (metadata)
    GET /stream/:type/:id.json                (stream resolution)

Plus a Gradio UI at /ui for manual testing.

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
import gradio as gr
import uvicorn

# Local imports - make sure 'providers' and 'utils' are importable
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from providers import flixlatam
from providers import latanime
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
    ]
    return {
        "id": ADDON_ID,
        "version": ADDON_VERSION,
        "name": ADDON_NAME,
        "description": "Streaming en Español y Latino desde FlixLatam + Latanime. "
                       "Catálogos completos + resolutor de streams.",
        "logo": "https://flixlatam.com/images/logo.png",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie", "series"],
        "idPrefixes": ["tt", "flixlatam:", "latanime:"],
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
        # parts[0] == "latanime", parts[1] == slug, parts[2]=season, parts[3]=episode
        if len(parts) >= 4:
            slug = parts[1]
            # season is parts[2], episode is parts[3]
            episode = int(parts[3])
            streams = latanime.get_episode_streams(slug, episode)

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
# Gradio UI for manual testing
# ============================================================
def _ui_resolve_movie(imdb_or_slug: str) -> str:
    if not imdb_or_slug.strip():
        return "Pon un IMDb ID (tt0816692) o un slug flixlatam (sultana-hUFIlu)"
    s = imdb_or_slug.strip()
    if s.startswith("flixlatam:"):
        s = s[len("flixlatam:"):]
    if s.startswith("tt"):
        streams = flixlatam.resolve_movie_streams(s)
    else:
        info = flixlatam.get_movie_detail(s)
        if not info:
            return f"No se encontro la pelicula con slug={s}"
        if not info.get("imdb_id"):
            return f"Pelicula encontrada pero sin IMDb ID: {info.get('name')}"
        streams = flixlatam.resolve_movie_streams(info["imdb_id"])
    return _format_streams(streams)


def _ui_resolve_episode(imdb_or_slug: str, season: int, episode: int) -> str:
    s = imdb_or_slug.strip()
    if not s:
        return "Pon un IMDb ID (tt45403168) o un slug flixlatam (en-coma-BVwton)"
    if s.startswith("flixlatam:"):
        s = s[len("flixlatam:"):]
    if s.startswith("tt"):
        streams = flixlatam.resolve_episode_streams(s, int(season), int(episode))
    else:
        ep_imdb = flixlatam.get_episode_imdb_id(s, int(season), int(episode))
        if not ep_imdb:
            return f"No se encontro el episodio T{season}E{episode} para slug={s}"
        m = re.match(r"(tt\d+)", ep_imdb)
        if m:
            streams = flixlatam.resolve_episode_streams(
                m.group(1), int(season), int(episode)
            )
        else:
            streams = []
    return _format_streams(streams)


def _format_streams(streams: list[dict]) -> str:
    if not streams:
        return "Sin streams disponibles"
    out = [f"Se resolvieron {len(streams)} streams:\n"]
    for s in streams:
        out.append(f"  • {s['name']}")
        out.append(f"    {s['url']}\n")
    return "\n".join(out)


def _ui_browse(page: int, kind: str) -> str:
    page = max(1, int(page))
    if kind == "Peliculas":
        items = flixlatam.get_movies(page=page)
    elif kind == "Series":
        items = flixlatam.get_series(page=page)
    else:
        items = flixlatam.get_popular_movies(page=page)
    if not items:
        return f"Sin resultados en pagina {page}"
    out = [f"Pagina {page} — {len(items)} items:\n"]
    for it in items[:24]:
        out.append(f"  • [{it['type']}] {it['name']}")
        out.append(f"    slug: {it['slug']}")
        if it.get("poster"):
            out.append(f"    poster: {it['poster']}")
        out.append("")
    return "\n".join(out)


with gr.Blocks(title="StreamFlix Stremio Addon") as demo:
    gr.Markdown(f"""
    # StreamFlix Reborn — Stremio Addon

    **URL del addon para Stremio:**
    ```
    <tu-url>/manifest.json
    ```

    • **FlareSolverr:** {'✅ activo' if flaresolverr_enabled() else '❌ desactivado (solo FlixLatam)'}
    • **TMDB API key:** {'✅' if TMDB_API_KEY else '❌ no configurada'}
    """)

    with gr.Tab("Resolver Película"):
        gr.Markdown("Pon un IMDb ID (`tt0816692`) o un slug FlixLatam (`sultana-hUFIlu`).")
        in_movie = gr.Textbox(label="IMDb ID o slug", placeholder="tt41228546")
        btn_movie = gr.Button("Resolver", variant="primary")
        out_movie = gr.Textbox(label="Streams", lines=12)
        btn_movie.click(_ui_resolve_movie, in_movie, out_movie)

    with gr.Tab("Resolver Episodio"):
        with gr.Row():
            in_ep_id = gr.Textbox(label="IMDb ID o slug", placeholder="tt45403168")
            in_ep_s = gr.Number(label="T", value=1, precision=0)
            in_ep_e = gr.Number(label="E", value=1, precision=0)
        btn_ep = gr.Button("Resolver", variant="primary")
        out_ep = gr.Textbox(label="Streams", lines=12)
        btn_ep.click(_ui_resolve_episode, [in_ep_id, in_ep_s, in_ep_e], out_ep)

    with gr.Tab("Explorar Catálogo"):
        with gr.Row():
            in_page = gr.Number(label="Página", value=1, precision=0)
            in_kind = gr.Radio(
                choices=["Peliculas", "Series", "Populares"],
                value="Peliculas",
                label="Tipo",
            )
        btn_browse = gr.Button("Cargar", variant="primary")
        out_browse = gr.Textbox(label="Catálogo", lines=20)
        btn_browse.click(_ui_browse, [in_page, in_kind], out_browse)


app = gr.mount_gradio_app(app, demo, path="/ui")


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    log.info(f"Starting StreamFlix Stremio addon on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
