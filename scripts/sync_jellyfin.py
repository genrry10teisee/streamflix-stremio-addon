#!/usr/bin/env python3
"""
StreamFlix → Jellyfin/Emby sync script.

Downloads .strm files from the StreamFlix server into a local folder
structure that Jellyfin/Emby can scan as a media library.

Usage:
  python sync_jellyfin.py --server https://streamflix-addon.onrender.com --token success --output /jellyfin/movies

For series:
  python sync_jellyfin.py --server https://streamflix-addon.onrender.com --token success --output /jellyfin/series --type series
"""

import argparse
import os
import re
import sys
import json
import time
import requests


def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.strip().rstrip('.')[:200]


def fetch_library(server, token, media_type, page, search):
    url = f"{server}/library/{media_type}.json"
    params = {"page": page}
    if search: params["search"] = search
    if token: params["token"] = token
    print(f"Fetching {url}...")
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        print(f"Error: HTTP {r.status_code}: {r.text[:200]}")
        sys.exit(1)
    return r.json()


def download_strm(server, token, strm_url, output_path):
    full_url = f"{server}{strm_url}"
    if token:
        sep = "&" if "?" in full_url else "?"
        full_url += f"{sep}token={token}"
    try:
        r = requests.get(full_url, timeout=60)
        if r.status_code != 200:
            return False
        stream_url = r.text.strip()
        if not stream_url.startswith("http"):
            return False
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            f.write(stream_url)
        return True
    except:
        return False


def sync_movies(server, token, output_dir, page, search, limit, dry_run):
    data = fetch_library(server, token, "movies", page, search)
    movies = data.get("movies", [])
    print(f"\nFound {len(movies)} movies")
    synced = 0
    for m in movies[:limit]:
        name = m.get("name", "Unknown")
        strm_url = m.get("strm_url", "")
        filename = f"{sanitize_filename(name)}.strm"
        output_path = os.path.join(output_dir, filename)
        print(f"[{synced+1}/{min(len(movies),limit)}] {name}")
        if not dry_run:
            if download_strm(server, token, strm_url, output_path):
                synced += 1
                print(f"  ✓ {output_path}")
            time.sleep(0.5)
        else:
            print(f"  [DRY RUN] → {output_path}")
    print(f"\n✅ Synced: {synced}")


def sync_series(server, token, output_dir, page, search, limit, dry_run):
    data = fetch_library(server, token, "series", page, search)
    series_list = data.get("series", [])
    print(f"\nFound {len(series_list)} series")
    synced = 0
    for s in series_list[:limit]:
        name = s.get("name", "Unknown")
        item_id = s.get("id", "")
        series_dir = os.path.join(output_dir, sanitize_filename(name))
        print(f"[{synced+1}/{min(len(series_list),limit)}] {name}")
        meta_url = f"{server}/meta/series/{item_id}.json"
        if token: meta_url += f"?token={token}"
        try:
            r = requests.get(meta_url, timeout=60)
            if r.status_code != 200: continue
            videos = r.json().get("meta", {}).get("videos", [])
            print(f"  {len(videos)} episodes")
            for v in videos:
                ep_id = v.get("id", "")
                season = v.get("season", 1)
                episode = v.get("episode", 1)
                season_dir = os.path.join(series_dir, f"Season {season:02d}")
                strm_path = f"/strm/series/{ep_id}.strm"
                ep_filename = f"{sanitize_filename(name)} S{season:02d}E{episode:02d}.strm"
                output_path = os.path.join(season_dir, ep_filename)
                if not dry_run:
                    download_strm(server, token, strm_path, output_path)
                    time.sleep(0.3)
            synced += 1
        except Exception as e:
            print(f"  ✗ {e}")
    print(f"\n✅ Synced: {synced}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync StreamFlix to Jellyfin/Emby")
    parser.add_argument("--server", required=True)
    parser.add_argument("--token", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--type", choices=["movies", "series"], default="movies")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--search", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    
    server = args.server.rstrip("/")
    print(f"StreamFlix → Jellyfin Sync")
    print(f"Server: {server} | Output: {args.output} | Type: {args.type}\n")
    
    if args.type == "movies":
        sync_movies(server, args.token, args.output, args.page, args.search, args.limit, args.dry_run)
    else:
        sync_series(server, args.token, args.output, args.page, args.search, args.limit, args.dry_run)
