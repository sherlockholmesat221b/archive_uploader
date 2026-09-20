"""
kabooz source: download a Qobuz album via kabooz into a per-album staging
dir, then hand it to the normal scan -> enrich -> upload pipeline.

Reuses the lazily-created QobuzSession from enrichment/qobuz.py so there is
exactly one login/session for both downloading and enrichment.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from ..config import TEMP_DIR
from ..enrichment.qobuz import _get_session
from ..models import Release
from ..scanning import scan_directory


def fetch_album(
    ref: str,
    state,
    quality: Optional[str] = None,
) -> Optional[Tuple[Release, Path]]:
    """
    Download one Qobuz album (ID or URL). Returns (Release, staging_dir),
    or None if `state` says it's already uploaded (nothing is downloaded).
    Raises RuntimeError if kabooz is unavailable or any track failed.
    """
    sess = _get_session()
    if sess is None:
        raise RuntimeError("kabooz is not installed or not logged in (run kabooz login first)")

    _, album_id = sess.resolve_id(ref, "album")
    meta = sess.get_album(album_id)
    upc = getattr(meta, "upc", "") or ""
    title = getattr(meta, "display_title", album_id)

    # Dedupe before spending bandwidth (SQLite matches by UPC / qobuz_id).
    if any(k and state.is_uploaded(k) for k in (upc, album_id)):
        print(f"  = {title}: already uploaded, skipping")
        return None

    # Per-album staging dir. Never wiped up front: kabooz's .part files let
    # a retry resume instead of re-downloading.
    stage = TEMP_DIR / "kabooz" / album_id
    (stage / "album").mkdir(parents=True, exist_ok=True)

    print(f"  \u2193 {title} [{album_id}]")
    res = sess.download_album(
        album_id,
        quality=sess.resolve_quality(quality),
        dest_dir=stage / "album",
        save_cover_file=True,
        download_goodies=False,
        fetch_lyrics_flag=False,
        on_track_start=lambda t, i, n: print(f"    [{i}/{n}] {t}"),
    )
    if res.failed:  # never upload a partial album
        raise RuntimeError(f"{title}: {len(res.failed)} track(s) failed: {res.failed}")

    releases = scan_directory(stage)  # exactly one subdir -> one Release
    if not releases:
        raise RuntimeError(f"{title}: no FLAC files found after download")
    rel = releases[0]
    rel.upc = rel.upc or upc  # exact-match key for the Qobuz enrichment provider
    rel.provider_ids["qobuz"] = album_id  # used by upload_release() for qobuz_id dedupe
    return rel, stage
