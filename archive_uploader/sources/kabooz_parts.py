"""
Part-wise kabooz fetching (plan_release + fetch_part). Sits beside sources/kabooz.py,
whose whole-album fetch_album() is left untouched.

Reuses the lazily-created QobuzSession from enrichment/qobuz.py (one login for
downloading and enrichment). fetch_part() needs kabooz's download_album(track_ids=...).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Optional, Tuple

from ..config import TEMP_DIR
from ..enrichment.qobuz import _get_session
from ..models import Release
from ..parts import PART_CAP, ZIP_LIMIT, Part, Plan, plan_parts, track_refs_from_album
from ..scanning import detect_cover, scan_directory


def _sess():
    sess = _get_session()
    if sess is None:
        raise RuntimeError("kabooz is not installed or not logged in (run kabooz login first)")
    return sess


def stage_dir(album_id: str) -> Path:
    return TEMP_DIR / "kabooz" / album_id


def plan_release(
    ref: str,
    quality: Optional[str] = None,
    part_cap: int = PART_CAP,
    zip_limit: int = ZIP_LIMIT,
):
    """Resolve a Qobuz ID/URL and split it into Parts. No audio is downloaded."""
    sess = _sess()
    _, album_id = sess.resolve_id(ref, "album")
    album = sess.get_album(album_id)  # kabooz's get_album pages up to 1200 tracks
    q = sess.resolve_quality(quality)
    refs = track_refs_from_album(album, q.name)
    if not refs:
        raise RuntimeError(f"{album_id}: album has no tracks")
    return album_id, album, plan_parts(refs, part_cap=part_cap, zip_limit=zip_limit)


def fetch_part(
    album_id: str,
    album,
    part: Part,
    quality: Optional[str] = None,
    goodies: bool = False,
    on_track: Optional[Callable] = None,
) -> Tuple[Release, Path]:
    """
    Download exactly the tracks in `part` into the album's staging dir and
    return (Release, staging_dir). The Release only contains this Part's
    tracks, but rel.dir_or_file is always the ALBUM ROOT, so IA file keys
    ("Disc 2/01.flac") are identical no matter which Part produced them.

    Goodies (booklets) are off by default, matching fetch_album(). Raises if
    any track failed, so a partial Part is never uploaded.
    """
    sess = _sess()
    stage = stage_dir(album_id)
    (stage / "album").mkdir(parents=True, exist_ok=True)  # .part files survive retries

    res = sess.download_album(
        album_id,
        quality=sess.resolve_quality(quality),
        dest_dir=stage / "album",
        save_cover_file=True,
        download_goodies=goodies,
        track_ids=part.track_ids,
        on_track_start=on_track or (lambda t, i, n: print(f"    [{i}/{n}] {t}")),
    )
    if res.failed:
        raise RuntimeError(f"{part.label}: {len(res.failed)} track(s) failed: {res.failed}")

    releases = scan_directory(stage)
    if not releases:
        raise RuntimeError(f"{part.label}: no FLAC files found after download")
    rel = releases[0]

    parents = {t.path.parent for t in rel.tracks if t.path}
    if parents:
        album_dir = Path(os.path.commonpath([str(p) for p in parents]))
        # A Part holding a single disc of a multi-disc album has the disc
        # folder as its common path; step up so keys keep their disc prefix.
        if (getattr(album, "media_count", 1) or 1) > 1 and album_dir in parents:
            album_dir = album_dir.parent
        if album_dir.is_dir() and album_dir != rel.dir_or_file:
            rel.dir_or_file = album_dir
            rel.cover_path = None
            detect_cover(rel)

    rel.upc = rel.upc or (getattr(album, "upc", "") or "")
    rel.provider_ids["Qobuz"] = album_id
    return rel, stage


def clear_part(album_id: str, part: Part) -> None:
    """Delete this Part's audio (and derived opus) but keep the staging root
    and cover so the next Part lands in the same album folder."""
    stage = stage_dir(album_id)
    for p in stage.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".flac", ".opus", ".part"):
            p.unlink(missing_ok=True)
    for d in sorted((p for p in stage.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()  # only removes now-empty disc dirs
        except OSError:
            pass


def cleanup_release(album_id: str) -> None:
    shutil.rmtree(stage_dir(album_id), ignore_errors=True)
