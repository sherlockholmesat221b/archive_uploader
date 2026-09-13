"""Last.fm enrichment provider for archive_uploader.

Last.fm has no keyless read endpoint — an API key is required. Get one
free at https://www.last.fm/api/account/create, then configure via
~/.config/archive_uploader/secrets.json:
    {"lastfm": {"api_key": "your-api-key"}}
or the ARCHIVE_UPLOADER_LASTFM_API_KEY environment variable. With no
key configured, fetch() always returns None (never raises).
"""
from __future__ import annotations

from typing import Optional

import requests

from ..config import get_secret
from ..models import Release
from .base import Provider

API_URL = "https://ws.audioscrobbler.com/2.0/"


class LastFmProvider(Provider):
    name = "Last.fm"
    logo_url = "https://www.last.fm/favicon.ico"

    def fetch(self, rel: Release) -> Optional[dict]:
        api_key = get_secret("lastfm", "api_key")
        if not api_key or not rel.artist or not rel.title:
            return None

        params = {
            "method": "album.getinfo",
            "artist": rel.artist,
            "album": rel.title,
            "api_key": api_key,
            "format": "json",
        }
        try:
            res = requests.get(API_URL, params=params, timeout=8)
            if res.status_code != 200:
                return None
            album = res.json().get("album")
            if not album:
                return None
        except Exception as e:
            print(f"  ! Last.fm fetch failed: {e}")
            return None

        result: dict = {"url": album.get("url", "")}
        if album.get("mbid"):
            result["id"] = album["mbid"]
        else:
            # Last.fm album ids aren't stable/public the way an mbid is —
            # fall back to the url itself so provider_ids still has *something*.
            result["id"] = album.get("url", "")

        tags = (album.get("tags") or {}).get("tag") or []
        if tags:
            result["genre"] = tags[0].get("name", "")

        return result
