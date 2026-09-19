# StreamFlix Reborn — Stremio Addon (Multi-Provider)

Addon de Stremio que replica el comportamiento de la app StreamFlix Reborn
usando múltiples proveedores latinos. Incluye catálogos completos y
resolución de streams en Español/Latino/Subtitulado.

## Proveedores soportados

| Proveedor | Tipo | Estado | Notas |
|---|---|---|---|
| **FlixLatam** (`flixlatam.com`) | Películas + Series | ✅ Funcional | Catálogo completo, 3 idiomas (LAT/CAST/SUB), IMDb IDs |
| **Latanime** (`latanime.org`) | Anime | ✅ Funcional | Catálogo de anime en Castellano/Latino, 8 hosts por episodio |
| **SoloLatino** (`sololatino.net`) | Películas + Series | ⚠️ Requiere FlareSolverr | Cloudflare Turnstile bloquea IPs cloud; funciona con proxy residencial |

## Características

- ✅ **Multi-provider**: FlixLatam + Latanime (SoloLatino opcional con FlareSolverr)
- ✅ **Catálogos completos** en Stremio (películas, series, anime)
- ✅ **Búsqueda** integrada
- ✅ **Resolución de streams** vía PoW + AES (extraído del código decompilado)
- ✅ **Extractores** para vidhide, mixdrop, mp4upload, streamwish, voe, hexload, mega
- ✅ **Soporte opcional para FlareSolverr** (para activar SoloLatino.net)
- ✅ **UI de prueba** en `/ui` para resolver streams manualmente
- ✅ **100% Python**, sin JS en el server (excepto unpacker Dean Edwards)
- ✅ **Docker** listo para Koyeb / Render / Fly.io / VPS

## Endpoints de Stremio

| Endpoint | Descripción |
|---|---|
| `GET /manifest.json` | Manifest del addon (4 catálogos) |
| `GET /catalog/movie/flixlatam_movies.json?page=N` | Películas FlixLatam |
| `GET /catalog/movie/flixlatam_popular.json?page=N` | Populares FlixLatam |
| `GET /catalog/series/flixlatam_series.json?page=N` | Series FlixLatam |
| `GET /catalog/series/latanime_catalog.json?page=N` | Anime Latanime |
| `GET /catalog/{type}/{id}.json?search=QUERY` | Búsqueda |
| `GET /meta/movie/flixlatam:{slug}.json` | Metadata de película |
| `GET /meta/series/flixlatam:{slug}.json` | Metadata de serie (+ episodios) |
| `GET /meta/series/latanime:{slug}.json` | Metadata de anime (+ episodios) |
| `GET /stream/movie/tt0816692.json` | Streams por IMDb ID |
| `GET /stream/series/tt45403168:1:1.json` | Streams de episodio por IMDb ID |
| `GET /stream/series/latanime:{slug}:1:1.json` | Streams de episodio Latanime |
| `GET /stream/movie/flixlatam:{slug}.json` | Streams de película FlixLatam |
| `GET /ui` | Interfaz web de prueba |

## Deploy en Koyeb (recomendado, gratis, no duerme)

### 1. Subir el código a GitHub

Crea un repo público o privado con estos archivos en la raíz:
- `app.py`
- `requirements.txt`
- `Dockerfile`
- `providers/` (carpeta completa con `flixlatam.py` y `latanime.py`)
- `extractors/` (carpeta completa con `hosts.py`)
- `utils/` (carpeta completa con `embed69_solver.py`, `flaresolverr.py`, `cf_bypass.py`, `nodriver_bypass.py`)

### 2. Crear el servicio en Koyeb

1. Ve a https://app.koyeb.com/ → **Create Service**
2. **Builder** → GitHub → elige el repo
3. **Branch:** `main`
4. **Buildpack:** Docker (autodetectado)
5. **Instance:** Free (eco, 512MB RAM)
6. **Region:** cualquiera
7. **Environment Variables** (opcional pero recomendado):
   - `TMDB_API_KEY` = tu key de TMDB (para richer metadata)
   - `FLARESOLVERR_URL` = URL de tu FlareSolverr si lo tienes (opcional, para SoloLatino)
8. **Port:** `8000`, **Path:** `/`
9. **Create Service**

### 3. Verificar

```bash
# Manifest
curl https://TU-URL.koyeb.app/manifest.json

# Catálogo de anime
curl 'https://TU-URL.koyeb.app/catalog/series/latanime_catalog.json?page=1'

# Streams de un anime real
curl 'https://TU-URL.koyeb.app/stream/series/latanime:dragon-ball-daima-castellano:1:1.json'

# Streams de una película
curl 'https://TU-URL.koyeb.app/stream/movie/tt41228546.json'

# UI web
open https://TU-URL.koyeb.app/ui
```

### 4. Instalar en Stremio

1. Abre Stremio → **Addons** → **Add Addon**
2. Pega: `https://TU-URL.koyeb.app/manifest.json`
3. **Install** ✅

Verás los catálogos **"FlixLatam · Películas"**, **"FlixLatam · Populares"**,
**"FlixLatam · Series"** y **"Latanime · Anime"** en la pantalla principal de Stremio.

## Cómo funciona

### FlixLatam
```
Stremio → /stream/movie/tt41228546.json
  → GET flixlatam.com/vidurl/tt41228546/
  → Parse HTML: extract POW_CHALLENGE, POW_DIFFICULTY, POW_SALT
  → Solve PoW (SHA-256 prefix zeros)
  → Derive AES key from SHA-256(challenge + solution + salt)
  → Decrypt base64-encoded link → embed URL (morencius.com/embed/xxx)
  → Unpack Dean Edwards packed JS in embed page
  → Return master.m3u8 URL
```

### Latanime
```
Stremio → /stream/series/latanime:dragon-ball-daima-castellano:1:1.json
  → GET latanime.org/ver/dragon-ball-daima-castellano-episodio-1
  → Parse HTML: extract <a data-player="base64-..."> for each host
  → Decode base64 → embed URL (mixdrop, mp4upload, voe, etc.)
  → For each host, extract playable URL (mp4 or m3u8)
  → Return list of streams (one per host)
```

## Activar SoloLatino.net (opcional, requiere FlareSolverr)

SoloLatino.net está detrás de Cloudflare Turnstile, que bloquea IPs cloud.
Para usarlo necesitas un FlareSolverr corriendo aparte con un proxy residencial:

1. Deploy FlareSolverr en Render/Hetzner/VPS con proxy residencial:
   - https://github.com/FlareSolverr/FlareSolverr
   - Imagen Docker: `flaresolverr/flaresolverr:latest`
   - Puerto: 8191

2. En tu StreamFlix addon (Koyeb), añade la env var:
   ```
   FLARESOLVERR_URL=https://tu-flaresolverr.onrender.com
   ```

3. Reinicia el servicio. Los requests a SoloLatino pasarán por FlareSolverr.

> ⚠️ El soporte completo para SoloLatino (catálogo + scraping) no está
> implementado todavía — solo la infraestructura de FlareSolverr. Si lo
> necesitas, ábrelo como issue y lo añado.

## Solución de problemas

### "Sin streams disponibles" para una película
- El proveedor podría no tener esa película. Prueba con otra.
- Verifica en el sitio web del proveedor que la película exista y tenga servidores.
- Revisa los logs del servicio (Koyeb → tu servicio → Logs).

### Latanime search no filtra bien
- Latanime usa POST AJAX para buscar, que está bloqueado por Cloudflare.
- El addon devuelve la página 1 del catálogo cuando se busca (mejor que 0).
- Para encontrar un anime específico, navega por las páginas del catálogo.

### Los streams no cargan en Stremio
- Stremio a veces no soporta URLs HLS con ciertos parámetros.
- Abre la URL del m3u8 directamente en VLC para verificar que funciona.
- Si el m3u8 caduca (tiene token `?t=...`), Stremio puede fallar al cargarlo
  después de un tiempo. Es un problema conocido de los streams con token.

### Mega.nz no se reproduce en Stremio
- Stremio no soporta el protocolo de Mega nativamente.
- El link se muestra en la UI pero no se puede reproducir directamente.
- Usa otros hosts (mixdrop, mp4upload) para esos casos.

## Estructura del proyecto

```
streamflix-stremio-addon/
├── app.py                       # FastAPI + Gradio + Stremio endpoints
├── requirements.txt
├── Dockerfile
├── README.md
├── providers/
│   ├── __init__.py
│   ├── flixlatam.py             # Scraper de FlixLatam (películas + series)
│   └── latanime.py              # Scraper de Latanime (anime)
├── extractors/
│   ├── __init__.py
│   └── hosts.py                 # Extractores vidhide/mixdrop/mp4upload/etc
└── utils/
    ├── __init__.py
    ├── embed69_solver.py        # PoW + AES (extraído del código decompilado)
    ├── flaresolverr.py          # Cliente opcional de FlareSolverr
    ├── cf_bypass.py             # Bypass Cloudflare con Playwright (fallback)
    └── nodriver_bypass.py       # Bypass Cloudflare con nodriver (fallback)
```

## Licencia

MIT. Basado en el comportamiento de la app StreamFlix Reborn (código decompilado
estudiado para entender el algoritmo de PoW/AES). No se incluye código original.

## Agradecimientos

- **StreamFlix Reborn** team — la app original que inspiró este addon.
- **FlixLatam** — por mantener un sitio accesible sin Cloudflare estricto.
- **Latanime** — por tener un catálogo grande de anime en Castellano/Latino.
- **Dean Edwards** — por el packer de JS que usan todos los hosts de video.
