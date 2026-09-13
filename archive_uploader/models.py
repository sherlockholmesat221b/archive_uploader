"""
Shared data models. Kept dependency-free (no requests/mutagen imports
here) so any module can import models without dragging in IO libraries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class TrackFile:
    path: Path
    title: str = ""
    artist: str = ""
    # Explicit Internet Archive identifier. Empty means automatic resolution.
    identifier: str = ""
    album: str = ""
    date: str = ""
    tracknumber: str = ""
    isrc: str = ""
    composer: str = ""


@dataclass
class ExternalLink:
    """
    One badge in the description: a service's logo + a hyperlink to the
    matched page. Every enrichment provider produces zero or more of
    these instead of the description-builder hardcoding per-service ifs.
    """
    service: str          # e.g. "Qobuz", "MusicBrainz"
    url: str
    logo_url: str = ""


@dataclass
class Release:
    kind: str  # 'album' or 'single'
    dir_or_file: Path
    tracks: List[TrackFile] = field(default_factory=list)
    title: str = ""
    artist: str = ""
    # Explicit Internet Archive identifier. Empty means automatic resolution.
    identifier: str = ""
    date: str = ""
    source: str = "Local Tags"
    cover_url: str = ""
    cover_path: Optional[Path] = None

    # Metadata
    genre: str = ""
    label: str = ""
    upc: str = ""
    isrc: str = ""
    composer: str = ""
    copyright: str = ""
    audio_spec: str = ""
    external_description: str = ""
    wikipedia_article: str = ""

    # Provider-sourced identifiers/links, keyed by provider name
    provider_ids: Dict[str, str] = field(default_factory=dict)
    external_links: List[ExternalLink] = field(default_factory=list)

    # Full raw fetch() dict per provider, keyed by provider name (the same
    # keys used in provider_ids). This is what state/store.py's
    # qobuz_raw_json / mb_raw_json columns are meant to persist — it used
    # to only exist in the on-disk metadata_cache, never on the Release
    # itself, so uploader.py had nothing real to read at DB-write time.
    raw_provider_data: Dict[str, Any] = field(default_factory=dict)
