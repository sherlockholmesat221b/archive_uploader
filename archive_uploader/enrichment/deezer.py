"""Deezer enrichment provider for archive_uploader.

Deezer's search API is public/keyless for read access — no credential
setup needed, unlike Discogs/Last.fm.
"""
from __future__ import annotations

from typing import Optional

import requests

from ..models import Release
from .base import Provider

SEARCH_URL = "https://api.deezer.com/search/album"


class DeezerProvider(Provider):
    name = "Deezer"
    logo_url = "https://e-cdns-files.dzcdn.net/img/favicon.ico"

    def fetch(self, rel: Release) -> Optional[dict]:
        if not rel.artist or not rel.title:
            return None

        query = f'artist:"{rel.artist}" album:"{rel.title}"'
        try:
            res = requests.get(SEARCH_URL, params={"q": query, "limit": 1}, timeout=8)
            if res.status_code != 200:
                return None
            data = res.json().get("data", [])
            if not data:
                return None
            top = data[0]
        except Exception as e:
            print(f"  ! Deezer fetch failed: {e}")
            return None

        result: dict = {
            "id": str(top.get("id", "")),
            "url": top.get("link", ""),
        }
        cover = top.get("cover_xl") or top.get("cover_big")
        if cover:
            result["cover_url"] = cover

        return result
