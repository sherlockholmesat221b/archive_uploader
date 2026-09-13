"""Discogs enrichment provider for archive_uploader.

Auth is optional but recommended: unauthenticated Discogs API requests
are rate-limited much harder than token-authenticated ones. Configure
via ~/.config/archive_uploader/secrets.json:
    {"discogs": {"token": "your-personal-access-token"}}
or the ARCHIVE_UPLOADER_DISCOGS_TOKEN environment variable. With
neither set, fetch() still works, just slower/more likely to 429.
"""
from __future__ import annotations

from typing import Optional

import requests

from ..config import get_secret
from ..models import Release
from .base import Provider

SEARCH_URL = "https://api.discogs.com/database/search"


class DiscogsProvider(Provider):
    name = "Discogs"
    logo_url = "https://www.discogs.com/favicon.ico"

    def fetch(self, rel: Release) -> Optional[dict]:
        if not rel.artist or not rel.title:
            return None

        token = get_secret("discogs", "token")
        headers = {"User-Agent": "archive-flac-uploader/1.0"}
        params: dict = {"type": "release", "per_page": 1}
        if token:
            params["token"] = token

        if rel.upc:
            params["barcode"] = rel.upc
        else:
            params["artist"] = rel.artist
            params["release_title"] = rel.title

        try:
            res = requests.get(SEARCH_URL, params=params, headers=headers, timeout=8)
            if res.status_code != 200:
                return None
            results = res.json().get("results", [])
            if not results and rel.upc:
                # Barcode search came up empty — retry as a plain title/artist search.
                params.pop("barcode", None)
                params["artist"] = rel.artist
                params["release_title"] = rel.title
                res = requests.get(SEARCH_URL, params=params, headers=headers, timeout=8)
                results = res.json().get("results", []) if res.status_code == 200 else []
            if not results:
                return None
            top = results[0]
        except Exception as e:
            print(f"  ! Discogs fetch failed: {e}")
            return None

        uri = top.get("uri", "")
        result: dict = {
            "id": str(top.get("id", "")),
            "url": f"https://www.discogs.com{uri}" if uri else "",
        }
        genres = top.get("genre") or []
        if genres:
            result["genre"] = genres[0]
        labels = top.get("label") or []
        if labels:
            result["label"] = labels[0]

        return result
