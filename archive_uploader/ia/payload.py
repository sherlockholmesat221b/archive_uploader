"""Internet Archive payload and metadata builder."""

import hashlib
import html
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from archive_uploader import __version__
from archive_uploader.ia.identifiers import resolve_identifier
from archive_uploader.models import ExternalLink, Release
from archive_uploader.packaging import create_clean_zip, derive_opus_file, determine_file_key
from archive_uploader.state.backup import compute_script_hash, get_repo_ref
from archive_uploader.textutils import slugify
from archive_uploader.ui import progress

SYSTEM_EXCLUDES = {
    ".ds_store", "thumbs.db", "desktop.ini", "@eadir",
    ".git", ".gitignore", "__pycache__",
}

REPO_URL = "https://github.com/sherlockholmesat221b/archive_uploader"

DOMAIN_SERVICE_MAP = {
    "open.qobuz.com": "Qobuz",
    "qobuz.com": "Qobuz",
    "musicbrainz.org": "MusicBrainz",
    "open.spotify.com": "Spotify",
    "spotify.com": "Spotify",
    "discogs.com": "Discogs",
    "music.apple.com": "Apple Music",
    "itunes.apple.com": "Apple Music",
    "en.wikipedia.org": "Wikipedia",
    "wikipedia.org": "Wikipedia",
    "deezer.com": "Deezer",
    "tidal.com": "Tidal",
    "bandcamp.com": "Bandcamp",
    "youtube.com": "YouTube",
    "music.youtube.com": "YouTube Music",
    "soundcloud.com": "SoundCloud",
    "allmusic.com": "AllMusic",
    "last.fm": "Last.fm",
    "amazon.com": "Amazon",
}


def render_link_badge(link: ExternalLink) -> str:
    domain = urllib.parse.urlparse(link.url).netloc.lower().replace("www.", "")
    service = link.service or DOMAIN_SERVICE_MAP.get(domain, domain.capitalize())
    logo = link.logo_url or f"https://www.google.com/s2/favicons?domain={domain}&sz=32"

    return (
        f'<a href="{link.url}" target="_blank" rel="nofollow">'
        f'<img src="{logo}" width="16" height="16" alt="{service}"> {service}</a>'
    )


def is_valid_payload_file(p: Path) -> bool:
    if p.name.startswith("."):
        return False
    if p.name.lower() in SYSTEM_EXCLUDES:
        return False
    return True


def _split_tags(value: str) -> List[str]:
    """Split a human-editable semicolon-separated metadata field."""
    return [part.strip() for part in value.split(";") if part.strip()]


def _unique(values: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        value = str(value).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _track_isrc_pairs(rel: Release) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for index, track in enumerate(rel.tracks, start=1):
        if track.isrc:
            number = track.tracknumber or f"{index:02d}"
            pairs.append((str(number), track.isrc))
    if not pairs and rel.isrc:
        pairs.append(("release", rel.isrc))
    return pairs


def _track_isrcs(rel: Release) -> List[str]:
    return _unique([code for _, code in _track_isrc_pairs(rel)])


def _track_composers(rel: Release) -> List[str]:
    composers: List[str] = []
    for track in rel.tracks:
        composers.extend(_split_tags(track.composer))
    composers.extend(_split_tags(rel.composer))
    return _unique(composers)


def _track_contributors(rel: Release) -> List[str]:
    contributors: List[str] = []
    primary = {artist.casefold() for artist in _split_tags(rel.artist)}
    for track in rel.tracks:
        for artist in _split_tags(track.artist):
            if artist and artist.casefold() not in primary:
                contributors.append(artist)
    return _unique(contributors)


def _build_subjects(rel: Release) -> List[str]:
    """Build controlled, metadata-derived IA subjects; no keyword stuffing."""
    values = ["audio", "music", "FLAC", "lossless audio", rel.artist, rel.title]
    values.extend(_split_tags(rel.genre))
    values.extend(_split_tags(rel.styles))
    values.extend(_split_tags(rel.label))
    if rel.country:
        values.append(rel.country)
        values.append(f"Country: {rel.country}")
    if rel.release_type:
        values.append(f"Release type: {rel.release_type}")
    for composer in _track_composers(rel):
        values.append(composer)
    for artist_credit in _split_tags(rel.secondary_artists):
        # "Name (Role)" is useful in the description but is noisy as a tag.
        values.append(artist_credit.split(" (", 1)[0])
    values.extend(_track_contributors(rel))
    return _unique(values)


def build_ia_payload(
    rel: Release,
    collection: str,
    mediatype: str,
    known_identifiers: Optional[Any] = None,
    opus_bitrate: str = "192k",
    identifier: Optional[str] = None,
) -> Tuple[str, dict, Dict[str, str], List[Path]]:
    """Builds Internet Archive payload metadata and target file map."""
    base = f"{rel.artist} {rel.title}".strip() or rel.dir_or_file.name
    id_hash = hashlib.md5(base.encode("utf-8")).hexdigest()[:8]
    identifier = identifier or rel.identifier or resolve_identifier(base, id_hash, known_identifiers)

    # Previously: ensure_script_backup() tarred the whole package and
    # attached it as a file to every single item. Now just hash + link
    # to the exact commit on GitHub instead — same reproducibility
    # guarantee (you can always see exactly what code produced this
    # upload), without a multi-MB tarball duplicated on every item.
    script_hash = compute_script_hash()
    repo_ref = get_repo_ref()
    repo_link = f"{REPO_URL}/tree/{repo_ref}"

    temp_cleanup_files: List[Path] = []
    opus_map: Dict[Path, Path] = {}

    flac_list = [t.path for t in rel.tracks if t.path and t.path.exists()]
    if not flac_list and rel.dir_or_file.is_file():
        flac_list = [rel.dir_or_file]
    elif not flac_list and rel.dir_or_file.is_dir():
        flac_list = sorted([
            p for p in rel.dir_or_file.rglob("*.flac")
            if is_valid_payload_file(p)
        ])

    total_flacs = len(flac_list)
    if total_flacs > 0:
        for idx, flac_p in enumerate(flac_list, start=1):
            progress(f"🎵 Deriving {opus_bitrate} Opus", idx, total_flacs, suffix=f"  {flac_p.name}")
            try:
                opus_p = derive_opus_file(flac_p, bitrate=opus_bitrate)
                opus_map[flac_p] = opus_p
                # temp_cleanup_files.append(opus_p)
            except Exception as e:
                sys.stdout.write(f"\n      ! Error deriving Opus for {flac_p.name}: {e}\n")
    is_single = (
        rel.kind == "single"
        or getattr(rel, "is_single", False)
        or "(SINGLE)" in rel.title.upper()
        or " - SINGLE" in rel.title.upper()
        or len(flac_list) <= 1
    )

    desc: List[str] = [
        f"<b>{html.escape(rel.title)}</b> by <b>{html.escape(rel.artist)}</b><br><br>"
    ]

    if rel.date:
        desc.append(f"<b>Release Date:</b> {html.escape(rel.date)}<br>")
    if rel.country:
        desc.append(f"<b>Country:</b> {html.escape(rel.country)}<br>")
    if rel.genre:
        desc.append(f"<b>Genre:</b> {html.escape(rel.genre)}<br>")
    if rel.styles:
        desc.append(f"<b>Styles:</b> {html.escape(rel.styles)}<br>")
    if rel.label:
        desc.append(f"<b>Label / Publisher:</b> {html.escape(rel.label)}<br>")
    if rel.catalog_number:
        desc.append(f"<b>Catalog number:</b> {html.escape(rel.catalog_number)}<br>")
    if rel.format:
        desc.append(f"<b>Release format:</b> {html.escape(rel.format)}<br>")
    if rel.release_type:
        desc.append(f"<b>Release type:</b> {html.escape(rel.release_type)}<br>")
    if rel.release_status:
        desc.append(f"<b>Release status:</b> {html.escape(rel.release_status)}<br>")
    if rel.upc:
        desc.append(f"<b>UPC / Barcode:</b> {html.escape(rel.upc)}<br>")
    track_isrcs = _track_isrcs(rel)
#    if track_isrcs:
#       desc.append("<b>Track ISRCs:</b><br>")
#      for number, code in _track_isrc_pairs(rel):
#            desc.append(f"{html.escape(number)}. {html.escape(code)}<br>")
    if rel.audio_spec:
        desc.append(f"<b>Format:</b> FLAC Lossless ({html.escape(rel.audio_spec)})<br>")
    composers = _track_composers(rel)
    if composers:
        desc.append(f"<b>Composers:</b> {html.escape('; '.join(composers))}<br>")
    contributors = _unique(_split_tags(rel.secondary_artists) + _track_contributors(rel))
    if contributors:
        desc.append(f"<b>Additional artists / credits:</b> {html.escape('; '.join(contributors))}<br>")
    if rel.copyright:
        desc.append(f"<b>Copyright / provenance:</b> {html.escape(rel.copyright)}<br>")

    files_dict: Dict[str, str] = {}
    slug_name = slugify(base)
    flac_zip_name = f"{slug_name}-flac-complete.zip"
    opus_zip_name = f"{slug_name}-opus-complete.zip"

    if not is_single:
        temp_zip_dir = Path(tempfile.gettempdir()) / "archive_uploader_zips"
        temp_zip_dir.mkdir(exist_ok=True)

        flac_zip_path = temp_zip_dir / flac_zip_name
        opus_zip_path = temp_zip_dir / opus_zip_name

        print("   📦 Packaging FLAC ZIP archive...")
        create_clean_zip(rel, flac_zip_path, exclude_ext=".opus", opus_map=opus_map)
        print("   ✓ FLAC ZIP ready")

        print("   📦 Packaging Opus ZIP archive...")
        create_clean_zip(rel, opus_zip_path, exclude_ext=".flac", opus_map=opus_map)
        print("   ✓ Opus ZIP ready")

        temp_cleanup_files.extend([flac_zip_path, opus_zip_path])

        desc.append("<br><b>Direct Custom Downloads (Complete Folders &amp; Artwork):</b><br>")
        desc.append(
            f'• <a href="https://archive.org/download/{identifier}/{flac_zip_name}">Download Full Album (FLAC Lossless + Docs)</a><br>'
        )
        desc.append(
            f'• <a href="https://archive.org/download/{identifier}/{opus_zip_name}">Download Full Album ({opus_bitrate} Opus + Docs)</a><br>'
        )

        files_dict[flac_zip_name] = str(flac_zip_path)
        files_dict[opus_zip_name] = str(opus_zip_path)
    else:
        print("   ℹ️  Single detected: Skipping ZIP archive creation.")

    if rel.kind == "album" and rel.tracks and not is_single:
        desc.append("<br><b>Tracklist:</b><br><ol>")
        for t in rel.tracks:
            track_str = html.escape(t.title)
            if t.artist and t.artist.lower() != rel.artist.lower():
                track_str += f" — {html.escape(t.artist)}"
            if t.composer:
                track_str += f" (Comp. {html.escape(t.composer)})"
            if t.isrc:
                track_str += f" [ISRC: {html.escape(t.isrc)}]"
            desc.append(f"<li>{track_str}</li>")
        desc.append("</ol>")

    if rel.external_description:
        desc.append(f"<br><b>Album Description:</b><br>{rel.external_description}<br>")

    if rel.wikipedia_article:
        desc.append(f"<br><b>Article Summary:</b><br>{rel.wikipedia_article}<br>")

    if rel.provider_ids:
        desc.append("<br><b>External release identifiers:</b><br>")
        for provider, value in sorted(rel.provider_ids.items()):
            desc.append(f"<b>{html.escape(provider)}:</b> {html.escape(str(value))}<br>")

    if rel.external_links:
        badges = [render_link_badge(link) for link in rel.external_links]
        desc.append("<br><b>External Links:</b><br>" + " | ".join(badges))

    desc.append(
        f'<br><br><i>Uploaded by <a href="{repo_link}" target="_blank" rel="nofollow">'
        f'archive_uploader v{__version__}</a> ({script_hash[:8]})</i><br>'
    )

    for flac_p in flac_list:
        key = determine_file_key(flac_p, rel)
        files_dict[key] = str(flac_p)

    for flac_p, opus_p in opus_map.items():
        if opus_p.exists():
            key = determine_file_key(opus_p, rel)
            files_dict[key] = str(opus_p)

    if rel.kind == "album" and rel.dir_or_file.is_dir():
        for extra in rel.dir_or_file.rglob("*"):
            if extra.is_file() and is_valid_payload_file(extra):
                if extra.suffix.lower() not in (".flac", ".opus"):
                    key = determine_file_key(extra, rel)
                    if key not in files_dict:
                        files_dict[key] = str(extra)

    if rel.cover_path and rel.cover_path.exists():
        ext = rel.cover_path.suffix.lower()
        cover_key = f"cover{ext}"
        if cover_key not in files_dict and rel.cover_path.name not in files_dict:
            files_dict[cover_key] = str(rel.cover_path)

    ext_ids: List[str] = []
    for pid, val in rel.provider_ids.items():
        if val:
            ext_ids.append(f"urn:{pid}:{val}")
    if rel.upc:
        ext_ids.append(f"urn:upc:{rel.upc}")
    for code in _track_isrcs(rel):
        ext_ids.append(f"urn:isrc:{code}")

    subject_tags = _build_subjects(rel)
    track_isrcs = _track_isrcs(rel)
    composers = _track_composers(rel)

    metadata: Dict[str, Any] = {
        "title": rel.title,
        "creator": rel.artist,
        "mediatype": mediatype,
        "collection": collection,
        "date": rel.date or "",
        "description": "".join(desc),
        "subject": subject_tags,
        "uploader_version": f"archive_uploader v{__version__}",
        "uploader_script_sha256": script_hash,
        "uploader_repo": repo_link,
    }

    # Keep release-level fields explicit so IA's metadata search has
    # machine-readable values in addition to the generated description.
    for provider, value in rel.provider_ids.items():
        if value:
            # Stable, readable keys such as musicbrainz_id/discogs_id.
            metadata[f"{provider.lower().replace(' ', '_').replace('-', '_')}_id"] = str(value)

    if rel.artist:
        metadata["artist"] = rel.artist
    if rel.title:
        metadata["album"] = rel.title
    if rel.country:
        metadata["country"] = rel.country
        metadata["coverage"] = rel.country
    if rel.genre:
        metadata["genre"] = _split_tags(rel.genre)
    if rel.styles:
        metadata["style"] = _split_tags(rel.styles)
    if rel.label:
        metadata["publisher"] = rel.label
        metadata["label"] = rel.label
    if rel.catalog_number:
        metadata["catalog_number"] = rel.catalog_number
    if rel.format:
        metadata["release_format"] = rel.format
    if rel.release_type:
        metadata["release_type"] = rel.release_type
    if rel.release_status:
        metadata["release_status"] = rel.release_status
    if rel.upc:
        metadata["upc"] = rel.upc
        metadata["barcode"] = rel.upc
    if track_isrcs:
        # All track ISRCs, not just the first/release-level one.
        metadata["isrc"] = track_isrcs
        metadata["track_isrc"] = [
            f"{number}: {code}" for number, code in _track_isrc_pairs(rel)
        ]
    if composers:
        metadata["composer"] = composers
    if contributors:
        metadata["contributor"] = contributors

    if ext_ids:
        metadata["external-identifier"] = sorted(list(set(ext_ids)))
    if rel.external_links:
        metadata["external_link"] = sorted(list(set(link.url for link in rel.external_links)))

    return identifier, metadata, files_dict, temp_cleanup_files
