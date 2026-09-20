from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Tuple

from ..config import CACHE_DIR
from ..models import ExternalLink, Release
from .base import Provider
from .isrc_ifpi import IsrcIfpiProvider
from .lastfm import LastFmProvider
from .musicbrainz import MusicBrainzProvider
from .overrides import apply_overrides, load_overrides
from .qobuz import QobuzProvider
from .discogs import DiscogsProvider
from .wikipedia import WikipediaProvider
from ..ui import info
from .parallel import iter_results

DEFAULT_PROVIDERS: List[Provider] = [
    QobuzProvider(), MusicBrainzProvider(), DiscogsProvider(), LastFmProvider(), WikipediaProvider(),
    # Link-only until isrc_ifpi.py is wired to a real endpoint — see its
    # docstring. Safe to leave enabled: it never overwrites a field, it
    # only adds a homepage badge when rel.isrc is already known.
    # IsrcIfpiProvider(),
]

# Fields a provider result dict may set directly on Release (besides id/url,
# which are handled specially — see enrich()).
_MERGE_FIELDS = (
    "genre", "label", "country", "catalog_number", "release_type",
    "release_status", "format", "styles", "secondary_artists", "upc",
    "external_description", "copyright", "audio_spec", "cover_url",
    "wikipedia_article",
)


def _release_cache_key(rel: Release) -> str:
    base = f"{rel.artist}-{rel.title}".strip() or rel.dir_or_file.name
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:12]


def _cache_raw(rel: Release, provider_name: str, data: dict) -> None:
    """Write-only cache of exactly what a provider returned. Never
    hand-edited — if you want to change a fetched value, use the
    override file (enrichment/overrides.py), not this."""
    cache_dir = CACHE_DIR / _release_cache_key(rel)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{provider_name.lower()}.json").write_text(
        json.dumps(data, default=str, indent=2)
    )


def enrich(rel: Release, providers: Optional[List[Provider]] = None) -> Release:
    """
    Merge order, weakest to strongest:
      1. local FLAC tags     (already on rel, from scanning.py)
      2. provider fetches    (this function)
      3. manual overrides    (always applied last, always win)
    Re-running enrichment can never clobber a hand-written override,
    because step 3 always runs after step 2 no matter what changed.

    Within step 2, the first provider (in list order) to set a given
    field wins — but if a later provider disagrees, that's logged
    instead of silently discarded, so a real mismatch between sources
    is something you actually see and can settle via the override file,
    rather than a coin flip decided by DEFAULT_PROVIDERS order.
    """
    providers = providers if providers is not None else DEFAULT_PROVIDERS
    print(f"\n[Enriching] {rel.artist} - {rel.title} ({rel.kind.upper()})")

    field_sources: Dict[str, Tuple[str, str]] = {}  # field -> (provider_name, value) that won

    for provider, result in iter_results(rel, providers):

        if not result:
            print(f"  · {provider.name}: no match / no usable metadata")
            continue

        _cache_raw(rel, provider.name, result)
        found = []
        if result.get("id"):
            found.append(f"id={result['id']}")
        if result.get("url"):
            found.append("link")
        for field in _MERGE_FIELDS:
            if result.get(field):
                found.append(field)
        info(f"✓ {provider.name}: " + (", ".join(found) if found else "response received"), 4)

        if result.get("id"):
            rel.provider_ids[provider.name] = result["id"]
        if result.get("url"):
            rel.external_links.append(
                ExternalLink(service=provider.name, url=result["url"], logo_url=provider.logo_url)
            )

        for field in _MERGE_FIELDS:
            value = result.get(field)
            if not value:
                continue
            if not getattr(rel, field, None):
                setattr(rel, field, value)
                field_sources[field] = (provider.name, value)
            elif field in field_sources and value != field_sources[field][1]:
                kept_provider, kept_value = field_sources[field]
                print(
                    f"  ~ {provider.name} also suggested {field}={value!r}; "
                    f"kept {kept_provider}'s {kept_value!r} (edit the override file to change it)"
                )

        # Providers such as Discogs can contribute track-level credits.
        # Match by 1-based position first, then by normalized title.
        for provider_track in result.get("tracks", []) or []:
            position = str(provider_track.get("position", "")).strip()
            target = None
            if position.isdigit():
                idx = int(position) - 1
                if 0 <= idx < len(rel.tracks):
                    target = rel.tracks[idx]
            if target is None:
                pt = str(provider_track.get("title", "")).strip().casefold()
                if pt:
                    target = next(
                        (t for t in rel.tracks if t.title.strip().casefold() == pt),
                        None,
                    )
            if target is None:
                continue
            for field in ("title", "artist", "composer"):
                value = provider_track.get(field)
                if value and not getattr(target, field, None):
                    setattr(target, field, value)

    apply_overrides(rel, load_overrides(rel))
    return rel
