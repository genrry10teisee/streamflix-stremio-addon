"""
Video host extractors.

Each extractor takes an embed URL (e.g. https://morencius.com/embed/XXX)
and returns a playable stream URL (m3u8 or mp4).

Supported hosts:
- vidhide (morencius.com, etc.) - Dean Edwards packed JS
- mixdrop (mixdrop.top, etc.) - Dean Edwards packed JS with MDCore object
- mp4upload (mp4upload.com) - videojs player, sources in JS
- streamwish (hglink.to, etc.) - heavily obfuscated JS, partial support
- voe (voe.sx, etc.) - heavily obfuscated JS, partial support
- hexload (hexload.com) - similar to mixdrop
- bysekoze - similar to streamwish
- mega (mega.nz) - returns public URL directly (Stremio doesn't play it but UI shows it)
- dsvplay - 403 from datacenter IPs
- savefiles - 403 from datacenter IPs
"""

import re
import base64
import logging
from typing import Optional
from urllib.parse import urljoin
import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ============================================================
# Dean Edwards packer unpacker (used by vidhide, mixdrop, etc.)
# ============================================================
def _unpack_dean_edwards(s: str) -> Optional[str]:
    """Unpack a Dean Edwards packed JS script."""
    m = re.search(
        r"function\(p,a,c,k,e,d\)\{[^}]+\}\('(.+?)',\s*(\d+),\s*(\d+),\s*'(.+?)'\.split\('\|'\)\)",
        s,
        re.DOTALL,
    )
    if not m:
        return None
    p = m.group(1)
    a = int(m.group(2))
    c = int(m.group(3))
    d = m.group(4).split("|")

    digits = "0123456789abcdefghijklmnopqrstuvwxyz"

    def to_base(n: int, radix: int) -> str:
        if n == 0:
            return "0"
        s = ""
        while n > 0:
            n, r = divmod(n, radix)
            s = digits[r] + s
        return s

    for i in range(c - 1, -1, -1):
        if i < len(d) and d[i]:
            token = to_base(i, a)
            p = re.sub(
                r"\b" + re.escape(token) + r"\b",
                lambda mm, repl=d[i]: repl,
                p,
            )
    return p


# ============================================================
# VidHide extractor
# ============================================================
VIDHIDE_DOMAINS = [
    "morencius.com", "vidhide.com", "vidhidepro.com", "vidhidehub.com",
    "vidhideart.com", "vidhidepremium.com", "thevidhub.com",
    "vidonline.site", "vidtube.com",
]


def extract_vidhide(embed_url: str) -> Optional[str]:
    """Extract the m3u8 URL from a VidHide embed page."""
    try:
        r = requests.get(
            embed_url,
            headers={"User-Agent": UA, "Referer": "https://flixlatam.com/"},
            timeout=15,
        )
        if r.status_code != 200:
            return None

        for s in re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL):
            if "function(p,a,c,k,e,d)" not in s:
                continue
            unpacked = _unpack_dean_edwards(s)
            if not unpacked:
                continue

            m3u8 = re.findall(r"https?://[^\s\"',<>]+\.m3u8[^\s\"',<>]*", unpacked)
            if m3u8:
                return m3u8[0]

            m = re.search(r'links\s*=\s*\{[^}]*"hls4"\s*:\s*"([^"]+)"', unpacked)
            if m:
                path = m.group(1)
                if path.startswith("http"):
                    return path
                return urljoin(embed_url, path)
        return None
    except Exception as e:
        log.warning(f"vidhide extract failed for {embed_url}: {e}")
        return None


# ============================================================
# MixDrop extractor (mixdrop.top, mixdrop.ag, etc.)
# ============================================================
MIXDROP_DOMAINS = [
    "mixdrop.top", "mixdrop.co", "mixdrop.ag", "mixdrop.bz",
    "mixdrop.ch", "mixdrop.nu", "mixdrop.mx", "mixdrop.to",
]


def extract_mixdrop(embed_url: str) -> Optional[str]:
    """Extract the mp4 URL from a MixDrop embed page."""
    try:
        r = requests.get(
            embed_url,
            headers={"User-Agent": UA, "Referer": "https://latanime.org/"},
            timeout=15,
        )
        if r.status_code != 200:
            return None

        # Method 1: unpack Dean Edwards packed JS
        for s in re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL):
            if "function(p,a,c,k,e,d)" not in s:
                continue
            unpacked = _unpack_dean_edwards(s)
            if not unpacked:
                continue
            # Look for MDCore.wurl="//...mp4?..."
            m = re.search(r'MDCore\.wurl\s*=\s*"([^"]+)"', unpacked)
            if m:
                url = m.group(1)
                if url.startswith("//"):
                    url = "https:" + url
                if not url.startswith("http"):
                    url = urljoin(embed_url, url)
                return url
            # Or look for any mp4 URL in unpacked
            mp4 = re.findall(r'https?:[^\s"\',<>]+\.mp4[^\s"\',<>]*', unpacked)
            if mp4:
                url = mp4[0]
                if url.startswith("//"):
                    url = "https:" + url
                return url

        # Method 2: direct mp4 in HTML
        mp4 = re.findall(r'https?://[^"\'\s<>]+\.mp4[^"\'\s<>]*', r.text)
        if mp4:
            return mp4[0]

        return None
    except Exception as e:
        log.warning(f"mixdrop extract failed for {embed_url}: {e}")
        return None


# ============================================================
# MP4Upload extractor
# ============================================================
MP4UPLOAD_DOMAINS = ["mp4upload.com", "mp4upload.org"]


def extract_mp4upload(embed_url: str) -> Optional[str]:
    """Extract the mp4 URL from an MP4Upload embed page."""
    try:
        r = requests.get(
            embed_url,
            headers={"User-Agent": UA, "Referer": "https://latanime.org/"},
            timeout=15,
        )
        if r.status_code != 200:
            return None

        # Method 1: look for sources: [{file: "...", label: "..."}]
        sources = re.findall(r'sources\s*:\s*\[\s*\{[^}]*["\']file["\']\s*:\s*["\']([^"\']+)["\']', r.text)
        if sources:
            return sources[0]

        # Method 2: look for | .mp4 | in script
        for s in re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL):
            if "function(p,a,c,k,e,d)" in s:
                unpacked = _unpack_dean_edwards(s)
                if unpacked:
                    mp4 = re.findall(r'https?:[^\s"\',<>]+\.mp4[^\s"\',<>]*', unpacked)
                    if mp4:
                        url = mp4[0]
                        if url.startswith("//"):
                            url = "https:" + url
                        return url
            # Direct mp4 in script
            mp4 = re.findall(r'https?://[^"\'\s<>]+\.mp4[^"\'\s<>]*', s)
            if mp4:
                # Filter out skin/css js
                url = mp4[0]
                if "videojs" not in url and "skins" not in url:
                    return url

        # Method 3: search for stream_url pattern
        m = re.search(r'stream[_-]?url\s*[:=]\s*["\']([^"\']+)["\']', r.text, re.I)
        if m:
            return m.group(1)

        return None
    except Exception as e:
        log.warning(f"mp4upload extract failed for {embed_url}: {e}")
        return None


# ============================================================
# StreamWish extractor
# ============================================================
STREAMWISH_DOMAINS = [
    "hglink.to", "streamwish.to", "streamwish.com",
    "sfastwish.com", "streamwish.tv", "embedwish.com",
]


def extract_streamwish(embed_url: str) -> Optional[str]:
    """Extract a playable URL from a StreamWish embed page (limited)."""
    try:
        r = requests.get(
            embed_url,
            headers={"User-Agent": UA, "Referer": "https://flixlatam.com/"},
            timeout=15,
        )
        if r.status_code != 200:
            return None

        # Direct URLs
        m3u8 = re.findall(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", r.text)
        if m3u8:
            return m3u8[0]
        mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", r.text)
        if mp4:
            return mp4[0]

        m = re.search(r"sources\s*:\s*\[\{[^}]*file\s*:\s*[\"']([^\"']+)[\"']", r.text)
        if m:
            url = m.group(1)
            if url.startswith("http"):
                return url

        # StreamWish serves heavily obfuscated JS — partial support
        return None
    except Exception as e:
        log.warning(f"streamwish extract failed for {embed_url}: {e}")
        return None


# ============================================================
# Voe extractor
# ============================================================
VOE_DOMAINS = [
    "voe.sx", "voe-unblock.com", "voeunblock.com", "voeunbl0ck.com",
    "v-o-e-unblock.com", "katherineschoolphone.com",
]


def extract_voe(embed_url: str) -> Optional[str]:
    """Extract a playable URL from a Voe embed page (limited)."""
    try:
        r = requests.get(
            embed_url,
            headers={"User-Agent": UA, "Referer": "https://flixlatam.com/"},
            timeout=15,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None

        # Follow JS redirect
        m = re.search(r"window\.location\.href\s*=\s*'([^']+)'", r.text)
        if m:
            new_url = m.group(1)
            if new_url.startswith("http"):
                r2 = requests.get(
                    new_url,
                    headers={"User-Agent": UA, "Referer": embed_url},
                    timeout=15,
                )
                if r2.status_code == 200:
                    m3u8 = re.findall(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", r2.text)
                    if m3u8:
                        return m3u8[0]
                    mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", r2.text)
                    if mp4:
                        return mp4[0]
                    for pat in [
                        r"\['mp4'\]\s*=\s*'([^']+)'",
                        r"\['hls'\]\s*=\s*'([^']+)'",
                        r"'file'\s*:\s*'(https?://[^']+\.(?:mp4|m3u8)[^']*)'",
                    ]:
                        mm = re.search(pat, r2.text)
                        if mm:
                            return mm.group(1)
        return None
    except Exception as e:
        log.warning(f"voe extract failed for {embed_url}: {e}")
        return None


# ============================================================
# HexLoad extractor (similar to mixdrop)
# ============================================================
HEXLOAD_DOMAINS = ["hexload.com"]


def extract_hexload(embed_url: str) -> Optional[str]:
    """Extract a playable URL from a HexLoad embed page."""
    try:
        r = requests.get(
            embed_url,
            headers={"User-Agent": UA, "Referer": "https://latanime.org/"},
            timeout=15,
        )
        if r.status_code != 200:
            return None

        for s in re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL):
            if "function(p,a,c,k,e,d)" in s:
                unpacked = _unpack_dean_edwards(s)
                if unpacked:
                    mp4 = re.findall(r'https?:[^\s"\',<>]+\.mp4[^\s"\',<>]*', unpacked)
                    if mp4:
                        url = mp4[0]
                        if url.startswith("//"):
                            url = "https:" + url
                        return url
                    m3u8 = re.findall(r'https?:[^\s"\',<>]+\.m3u8[^\s"\',<>]*', unpacked)
                    if m3u8:
                        url = m3u8[0]
                        if url.startswith("//"):
                            url = "https:" + url
                        return url
        # Direct
        mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", r.text)
        if mp4:
            return mp4[0]
        return None
    except Exception as e:
        log.warning(f"hexload extract failed for {embed_url}: {e}")
        return None


# ============================================================
# Mega extractor (returns URL as-is, Stremio can't play but UI shows)
# ============================================================
def extract_mega(embed_url: str) -> Optional[str]:
    """Return Mega URL as-is (Stremio can't play these but the link is shown)."""
    return embed_url


# ============================================================
# Streamtape extractor
# ============================================================
STREAMTAPE_DOMAINS = ["streamtape.com", "streamtape.to", "streamtape.net", "strtape.tech", "streamtapeadblock"]


def extract_streamtape(embed_url: str) -> Optional[str]:
    """Extract mp4 URL from Streamtape."""
    try:
        r = requests.get(embed_url, headers={"User-Agent": UA, "Referer": "https://tioanime.com/"}, timeout=15)
        if r.status_code != 200:
            return None
        # Look for target url in JS
        m = re.search(r"document\.getElementById\(['\"]videolink['\"]\)\.innerHTML\s*=\s*['\"]?([^'\"]+)['\"]?", r.text)
        if m:
            return f"https://streamtape.com/get_video?id={m.group(1)}"
        # Or directly look for mp4
        mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", r.text)
        if mp4:
            return mp4[0]
        # Or sources array
        m = re.search(r'sources\s*:\s*\[\{[^}]*src\s*:\s*["\']([^"\']+)["\']', r.text)
        if m:
            return m.group(1)
        return None
    except Exception as e:
        log.warning(f"streamtape extract failed for {embed_url}: {e}")
        return None


# ============================================================
# Vidoza extractor
# ============================================================
VIDOZA_DOMAINS = ["vidoza.net", "vidoza.co", "vidoza.com"]


def extract_vidoza(embed_url: str) -> Optional[str]:
    """Extract mp4 URL from Vidoza (looks for <source src=...>)."""
    try:
        r = requests.get(embed_url, headers={"User-Agent": UA, "Referer": "https://tioanime.com/"}, timeout=15)
        if r.status_code != 200:
            return None
        # Look for source tag with src
        for m in re.finditer(r'<source[^>]+src=["\']([^"\']+)["\']', r.text):
            return m.group(1)
        # Or sources array
        m = re.search(r'sources\s*:\s*\[\{[^}]*["\']src["\']\s*:\s*["\']([^"\']+)["\']', r.text)
        if m:
            return m.group(1)
        # Or src: in JS
        m = re.search(r'src\s*:\s*["\']([^"\']+\.(?:mp4|m3u8)[^"\']*)["\']', r.text)
        if m:
            return m.group(1)
        return None
    except Exception as e:
        log.warning(f"vidoza extract failed for {embed_url}: {e}")
        return None


# ============================================================
# YourUpload extractor
# ============================================================
YOURUPLOAD_DOMAINS = ["yourupload.com", "yucache.net"]


def extract_yourupload(embed_url: str) -> Optional[str]:
    """Extract mp4 URL from YourUpload (looks for file:'...mp4')."""
    try:
        r = requests.get(embed_url, headers={"User-Agent": UA, "Referer": "https://tioanime.com/"}, timeout=15)
        if r.status_code != 200:
            return None
        # Pattern from APK: file:\s*'([^']+\.(?:m3u8|mp4))'
        m = re.search(r"file\s*:\s*['\"]([^'\"]+\.(?:m3u8|mp4)[^'\"]*)['\"]", r.text)
        if m:
            return m.group(1)
        # Or source tag
        for m in re.finditer(r'<source[^>]+src=["\']([^"\']+)["\']', r.text):
            return m.group(1)
        # Or sources
        m = re.search(r'sources\s*:\s*\[\{[^}]*["\']file["\']\s*:\s*["\']([^"\']+)["\']', r.text)
        if m:
            return m.group(1)
        return None
    except Exception as e:
        log.warning(f"yourupload extract failed for {embed_url}: {e}")
        return None


# ============================================================
# Doodstream extractor (uses a hash table to decode video URL)
# ============================================================
DOODSTREAM_DOMAINS = ["doodstream.com", "dood.la", "dood.li", "dood.so", "dood.ws", "dood.yt",
                      "dood.pm", "dood.re", "dood.wf", "vide0.net"]


def extract_doodstream(embed_url: str) -> Optional[str]:
    """Extract mp4 URL from Doodstream."""
    try:
        r = requests.get(embed_url, headers={"User-Agent": UA, "Referer": "https://fanpelis.to/"}, timeout=15)
        if r.status_code != 200:
            return None
        # Find the download page token (md5 hash in function call)
        # Pattern: pass_md5/XXXX/XXXX
        m = re.search(r"'(/pass_md5/[^']+)'", r.text)
        if m:
            token_path = m.group(1)
            base = re.match(r"https?://[^/]+", embed_url).group(0)
            token_url = base + token_path
            r2 = requests.get(token_url, headers={"User-Agent": UA, "Referer": embed_url}, timeout=15)
            if r2.status_code == 200:
                # The token response is the partial URL, we need to add random chars + the video ID
                token = r2.text
                # Find video_id and length
                m2 = re.search(r"'(\d{10})'", r.text)
                if m2:
                    video_id = m2.group(1)
                    import random, string
                    random_str = "".join(random.choices(string.ascii_letters + string.digits, k=10))
                    final_url = f"{token}{random_str}?token={video_id}&expiry="
                    return final_url
        # Fallback: look for direct mp4
        mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", r.text)
        if mp4:
            return mp4[0]
        return None
    except Exception as e:
        log.warning(f"doodstream extract failed for {embed_url}: {e}")
        return None


# ============================================================
# Goodstream extractor
# ============================================================
GOODSTREAM_DOMAINS = ["goodstream.one"]


def extract_goodstream(embed_url: str) -> Optional[str]:
    """Extract playable URL from Goodstream."""
    try:
        r = requests.get(embed_url, headers={"User-Agent": UA, "Referer": "https://fanpelis.to/"}, timeout=15)
        if r.status_code != 200:
            return None
        # Look for sources or file
        m = re.search(r'sources\s*:\s*\[\{[^}]*["\']file["\']\s*:\s*["\']([^"\']+)["\']', r.text)
        if m:
            return m.group(1)
        m = re.search(r'file\s*:\s*["\']([^"\']+\.(?:mp4|m3u8)[^"\']*)["\']', r.text)
        if m:
            return m.group(1)
        # Packed JS?
        for s in re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL):
            if "function(p,a,c,k,e,d)" in s:
                unpacked = _unpack_dean_edwards(s)
                if unpacked:
                    mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", unpacked)
                    if mp4:
                        return mp4[0]
                    m3u8 = re.findall(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", unpacked)
                    if m3u8:
                        return m3u8[0]
                    # Look for file:
                    m2 = re.search(r'file\s*:\s*["\']([^"\']+)["\']', unpacked)
                    if m2:
                        return m2.group(1)
        return None
    except Exception as e:
        log.warning(f"goodstream extract failed for {embed_url}: {e}")
        return None


# ============================================================
# Veev extractor
# ============================================================
VEEV_DOMAINS = ["veev.to"]


def extract_veev(embed_url: str) -> Optional[str]:
    """Extract mp4 URL from Veev.to."""
    try:
        r = requests.get(embed_url, headers={"User-Agent": UA, "Referer": "https://fanpelis.to/"}, timeout=15)
        if r.status_code != 200:
            return None
        # Look for direct mp4 or m3u8
        mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", r.text)
        if mp4:
            return mp4[0]
        m3u8 = re.findall(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", r.text)
        if m3u8:
            return m3u8[0]
        # Sources
        m = re.search(r'sources\s*:\s*\[\{[^}]*["\']file["\']\s*:\s*["\']([^"\']+)["\']', r.text)
        if m:
            return m.group(1)
        # Packed JS?
        for s in re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL):
            if "function(p,a,c,k,e,d)" in s:
                unpacked = _unpack_dean_edwards(s)
                if unpacked:
                    mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", unpacked)
                    if mp4:
                        return mp4[0]
                    m3u8 = re.findall(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", unpacked)
                    if m3u8:
                        return m3u8[0]
        return None
    except Exception as e:
        log.warning(f"veev extract failed for {embed_url}: {e}")
        return None


# ============================================================
# Vimeos / LaMovie extractor (similar to goodstream)
# ============================================================
VIMEOS_DOMAINS = ["vimeos.net", "lamovie.link"]


def extract_vimeos(embed_url: str) -> Optional[str]:
    """Extract playable URL from Vimeos/LaMovie."""
    try:
        r = requests.get(embed_url, headers={"User-Agent": UA, "Referer": "https://fanpelis.to/"}, timeout=15)
        if r.status_code != 200:
            return None
        # Look for direct URLs
        mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", r.text)
        if mp4:
            return mp4[0]
        m3u8 = re.findall(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", r.text)
        if m3u8:
            return m3u8[0]
        # Sources
        m = re.search(r'sources\s*:\s*\[\{[^}]*["\']file["\']\s*:\s*["\']([^"\']+)["\']', r.text)
        if m:
            return m.group(1)
        # Packed JS?
        for s in re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.DOTALL):
            if "function(p,a,c,k,e,d)" in s:
                unpacked = _unpack_dean_edwards(s)
                if unpacked:
                    mp4 = re.findall(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*", unpacked)
                    if mp4:
                        return mp4[0]
                    m3u8 = re.findall(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", unpacked)
                    if m3u8:
                        return m3u8[0]
        return None
    except Exception as e:
        log.warning(f"vimeos extract failed for {embed_url}: {e}")
        return None


# ============================================================
# Dispatcher
# ============================================================
_EXTRACTORS = [
    ("vidhide", VIDHIDE_DOMAINS, extract_vidhide),
    ("mixdrop", MIXDROP_DOMAINS, extract_mixdrop),
    ("mp4upload", MP4UPLOAD_DOMAINS, extract_mp4upload),
    ("streamwish", STREAMWISH_DOMAINS, extract_streamwish),
    ("voe", VOE_DOMAINS, extract_voe),
    ("hexload", HEXLOAD_DOMAINS, extract_hexload),
    ("mega", ["mega.nz"], extract_mega),
    ("streamtape", STREAMTAPE_DOMAINS, extract_streamtape),
    ("vidoza", VIDOZA_DOMAINS, extract_vidoza),
    ("yourupload", YOURUPLOAD_DOMAINS, extract_yourupload),
    ("doodstream", DOODSTREAM_DOMAINS, extract_doodstream),
    ("goodstream", GOODSTREAM_DOMAINS, extract_goodstream),
    ("veev", VEEV_DOMAINS, extract_veev),
    ("vimeos", VIMEOS_DOMAINS, extract_vimeos),
]


def extract_stream(embed_url: str, host: str = "") -> Optional[str]:
    """
    Try to extract a playable URL from an embed URL.

    Args:
        embed_url: The embed URL (e.g. https://morencius.com/embed/xxx)
        host: Optional host name (e.g. 'vidhide', 'streamwish', 'voe')

    Returns:
        A playable m3u8/mp4 URL, or None if extraction failed.
    """
    host_lower = (host or "").lower()
    url_lower = embed_url.lower()

    # Match by host name first
    if host_lower:
        for name, domains, fn in _EXTRACTORS:
            if name in host_lower:
                return fn(embed_url)

    # Match by URL domain
    for name, domains, fn in _EXTRACTORS:
        if any(d in url_lower for d in domains):
            return fn(embed_url)

    # Fallback: try all extractors
    for fn in (extract_vidhide, extract_mixdrop, extract_mp4upload, extract_streamtape,
               extract_vidoza, extract_yourupload, extract_veev, extract_vimeos,
               extract_streamwish, extract_voe, extract_hexload, extract_goodstream,
               extract_doodstream):
        url = fn(embed_url)
        if url:
            return url
    return None
