"""
A hand-editable sidecar file per release. This is the answer to three
separate asks that turn out to be the same mechanism:
  - "I have rare items nothing online will ever match" -> write the
    fields here directly, the merge treats it identically either way.
  - "I want to edit metadata a provider fetched" -> edit the field
    here; pipeline.py always applies overrides last, so re-running
    enrichment can never clobber it.
  - "I want to micromanage every field, including per-track" -> every
    Release field is listed in SIMPLE_FIELDS, and every TrackFile field
    is editable under "tracks", keyed by filename (or 1-based track
    number as a fallback key, for when you'd rather not type filenames).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..models import ExternalLink, Release

OVERRIDE_FILENAME = ".archive_meta.json"

# Fields an override file may set directly on the Release.
SIMPLE_FIELDS = (
    "title", "artist", "date", "genre", "label", "upc", "isrc", "composer",
    "copyright", "audio_spec", "external_description", "wikipedia_article",
    "cover_url",
)

# Fields an override file may set directly on a single TrackFile, under
# the "tracks" key.
TRACK_FIELDS = (
    "title", "artist", "album", "date", "tracknumber", "isrc", "composer",
)


def override_path(rel: Release) -> Path:
    if rel.kind == "album":
        return rel.dir_or_file / OVERRIDE_FILENAME
    return rel.dir_or_file.with_name(rel.dir_or_file.stem + OVERRIDE_FILENAME)


def load_overrides(rel: Release) -> Dict[str, Any]:
    path = override_path(rel)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ! Could not read override file {path}: {e}")
        return {}


def _apply_track_overrides(rel: Release, track_overrides: Dict[str, Any]) -> None:
    by_filename = {t.path.name: t for t in rel.tracks}
    for key, fields in track_overrides.items():
        track = by_filename.get(key)
        if track is None and str(key).isdigit():
            idx = int(key) - 1
            if 0 <= idx < len(rel.tracks):
                track = rel.tracks[idx]
        if track is None:
            print(f"  ! Override for unknown track '{key}' skipped (no filename or track-number match)")
            continue
        for field in TRACK_FIELDS:
            if fields.get(field):
                setattr(track, field, fields[field])


def apply_overrides(rel: Release, overrides: Dict[str, Any]) -> None:
    for field in SIMPLE_FIELDS:
        if overrides.get(field):
            setattr(rel, field, overrides[field])

    for link in overrides.get("external_links", []):
        rel.external_links.append(
            ExternalLink(
                service=link.get("service", "Custom"),
                url=link.get("url", ""),
                logo_url=link.get("logo_url", ""),
            )
        )

    for key, value in overrides.get("provider_ids", {}).items():
        rel.provider_ids[key] = value

    track_overrides = overrides.get("tracks", {})
    if track_overrides:
        _apply_track_overrides(rel, track_overrides)


def write_starter_override(rel: Release) -> Path:
    """Drop a blank template next to a release so filling in metadata by
    hand — release-level AND per-track — is just editing JSON, not
    touching any code. Safe to call repeatedly: never overwrites an
    existing template, so hand-edits are never clobbered."""
    path = override_path(rel)
    if path.exists():
        return path
    template: Dict[str, Any] = {f: "" for f in SIMPLE_FIELDS}
    template["external_links"] = []
    template["provider_ids"] = {}
    template["tracks"] = {
        t.path.name: {f: "" for f in TRACK_FIELDS} for t in rel.tracks
    }
    path.write_text(json.dumps(template, indent=2))
    return path
