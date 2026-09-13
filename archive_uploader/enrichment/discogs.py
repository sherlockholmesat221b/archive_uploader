"""Discogs release enrichment.

Discogs' API requires authentication. Set ``DISCOGS_TOKEN`` to a personal
Discogs API token to enable this provider. Matching is deliberately
conservative: UPC/barcode is tried first, then artist/title search, and the
selected release is fetched in full.

The provider contributes release-level cataloguing metadata and track credits.
It does not overwrite local tags; pipeline.py owns merge precedence.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

import requests

from ..config import get_secret
from ..models import Release
from .base import Provider

API = "https://api.discogs.com"
USER_AGENT = "archive_uploader/2.0 (+https://github.com/sherlockholmesat221b/archive_uploader)"


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _join_unique(values: list[str]) -> str:
    seen = []
    for value in values:
        value = str(value).strip()
        if value and value not in seen:
            seen.append(value)
    return "; ".join(seen)


class DiscogsProvider(Provider):
    name = "Discogs"
    logo_url = "https://www.google.com/s2/favicons?domain=discogs.com&sz=32"

    def __init__(self) -> None:
        self.token = (
            os.environ.get("DISCOGS_TOKEN", "").strip()
            or os.environ.get("ARCHIVE_UPLOADER_DISCOGS_TOKEN", "").strip()
            or str(get_secret("discogs", "token") or "").strip()
        )
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        if self.token:
            self._session.headers["Authorization"] = f"Discogs token={self.token}"

    def _get(self, path: str, **params: Any) -> Optional[dict]:
        # Preserve the existing unauthenticated fallback; a personal token
        # is strongly preferred because Discogs rate-limits anonymous calls.
        response = self._session.get(f"{API}{path}", params=params, timeout=20)
        response.raise_for_status()
        return response.json()

    def _search(self, rel: Release) -> Optional[dict]:
        if rel.upc:
            data = self._get("/database/search", barcode=rel.upc, type="release", per_page=10)
            results = (data or {}).get("results", [])
            for result in results:
                barcodes = {str(x).strip() for x in result.get("barcode", []) if x}
                if rel.upc in barcodes:
                    return result
            if results:
                # Discogs sometimes indexes a barcode in a form different
                # from the normalized value returned by its API.
                return results[0]

        if not (rel.artist and rel.title):
            return None

        data = self._get(
            "/database/search",
            artist=rel.artist,
            release_title=rel.title,
            type="release",
            per_page=10,
        )
        results = (data or {}).get("results", [])
        wanted_artist = _norm(rel.artist)
        wanted_title = _norm(rel.title)

        # Prefer an exact normalized title/artist match, otherwise take the
        # first result only when it is clearly title-matched.
        for result in results:
            result_title = _norm(str(result.get("title", "")))
            if wanted_artist in result_title and wanted_title in result_title:
                return result
        for result in results:
            result_title = _norm(str(result.get("title", "")))
            if wanted_title and wanted_title in result_title:
                return result
        return results[0] if results else None

    @staticmethod
    def _track_artist(track: dict) -> str:
        artists = []
        for artist in track.get("artists", []) or []:
            name = artist.get("name")
            if name:
                artists.append(name)
        return _join_unique(artists)

    @staticmethod
    def _track_composer(track: dict) -> str:
        names = []
        for credit in track.get("extraartists", []) or []:
            role = str(credit.get("role", "")).casefold()
            if any(x in role for x in ("written-by", "composer", "composed by", "music by")):
                name = credit.get("name")
                if name:
                    names.append(name)
        return _join_unique(names)

    def fetch(self, rel: Release) -> Optional[dict]:
        try:
            hit = self._search(rel)
            if not hit or not hit.get("id"):
                return None

            release_id = str(hit["id"])
            data = self._get(f"/releases/{release_id}")
            if not data:
                return None

            result: dict[str, Any] = {
                "id": release_id,
                "url": f"https://www.discogs.com/release/{release_id}",
            }

            for key in ("title", "country", "released", "notes"):
                value = data.get(key)
                if value:
                    result[{
                        "title": "title",
                        "country": "country",
                        "released": "date",
                        "notes": "external_description",
                    }[key]] = value

            if data.get("year") and not result.get("date"):
                result["date"] = str(data["year"])

            release_artists = _join_unique([
                artist.get("name", "")
                for artist in data.get("artists", []) or []
                if artist.get("name")
            ])
            if release_artists:
                result["artist"] = release_artists

            genres = [str(x) for x in data.get("genres", []) if x]
            styles = [str(x) for x in data.get("styles", []) if x]
            if genres:
                result["genre"] = "; ".join(genres)
            if styles:
                result["styles"] = "; ".join(styles)

            labels = data.get("labels", []) or []
            label_names = [x.get("name", "") for x in labels if x.get("name")]
            catalog_numbers = [x.get("catno", "") for x in labels if x.get("catno") and x.get("catno") != "none"]
            if label_names:
                result["label"] = _join_unique(label_names)
            if catalog_numbers:
                result["catalog_number"] = _join_unique(catalog_numbers)

            formats = data.get("formats", []) or []
            format_parts = []
            for fmt in formats:
                name = fmt.get("name", "")
                qty = fmt.get("qty", "")
                descriptions = fmt.get("descriptions", []) or []
                piece = " ".join(str(x) for x in (qty, name) if x)
                if descriptions:
                    piece += f" ({', '.join(map(str, descriptions))})"
                if piece:
                    format_parts.append(piece)
            if format_parts:
                result["format"] = "; ".join(format_parts)

            # Discogs' release type/status are represented by its metadata;
            # keep explicit values when present, without inventing them.
            if data.get("status"):
                result["release_status"] = str(data["status"])
            if data.get("type"):
                result["release_type"] = str(data["type"])

            # Keep a compact list of non-primary artists/credits at release
            # level, while retaining the detailed track credits below.
            secondary = []
            for credit in data.get("extraartists", []) or []:
                name = credit.get("name")
                role = credit.get("role")
                if name:
                    secondary.append(f"{name} ({role})" if role else name)
            if secondary:
                result["secondary_artists"] = _join_unique(secondary)

            tracks = []
            for index, track in enumerate(data.get("tracklist", []) or [], start=1):
                title = str(track.get("title", "")).strip()
                if not title:
                    continue
                position = str(track.get("position") or index).strip()
                tracks.append({
                    "position": position,
                    "title": title,
                    "artist": self._track_artist(track),
                    "composer": self._track_composer(track),
                })
            if tracks:
                result["tracks"] = tracks

            print(f"  ✓ Discogs Match: {result['title']} (release {release_id})")
            return result

        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", "?")
            print(f"  ! Discogs API HTTP {status}; check your Discogs token/rate limit if needed")
            return None
        except Exception as e:
            print(f"  ! Discogs fetch failed: {e}")
            return None
