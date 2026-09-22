"""Deterministic audio transcoding and ZIP archive packaging tools."""

import hashlib
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, Set

from archive_uploader.models import Release

FIXED_ZIP_DATETIME = (1980, 1, 1, 0, 0, 0)
SYSTEM_EXCLUDES = {".ds_store", "thumbs.db", "desktop.ini", "@eadir"}


def is_valid_asset(p: Path) -> bool:
    """Checks if file is a valid asset and not a hidden OS metadata file."""
    if p.name.startswith("."):
        return False
    if p.name.lower() in SYSTEM_EXCLUDES:
        return False
    return True


# def derive_opus_file(flac_path: Path, bitrate: str = "192k") -> Path:
#    """Derives deterministic Opus audio using serial number calculated from input FLAC MD5 hash."""
#    opus_path = flac_path.with_suffix(".opus")
#    if not opus_path.exists() or opus_path.stat().st_size == 0:
#        if not shutil.which("opusenc"):
#            raise RuntimeError("opusenc command-line tool not found in PATH.")
#
#        kbps = bitrate.replace("k", "")
#        flac_md5 = hashlib.md5(flac_path.read_bytes()).hexdigest()
#        serial_num = int(flac_md5[:8], 16) & 0xFFFFFFFF
#
#        cmd = [
#            "opusenc",
#            "--quiet",
#            "--bitrate",
#            kbps,
#            "--serial",
#            str(serial_num),
#            str(flac_path),
#            str(opus_path),
#        ]
#        try:
#            subprocess.run(cmd, check=True, capture_output=True, text=True)
#        except subprocess.CalledProcessError as e:
#            print(f"  ! opusenc Error for {flac_path.name}:\n{e.stderr}")
#            if opus_path.exists():
#                opus_path.unlink()
#            raise e
#    return opus_path
#

def determine_file_key(file_path: Path, rel: Release) -> str:
    """Determines relative IA file key, preserving folder structure for multi-file items."""
    if rel.kind != "album" or not rel.dir_or_file.is_dir():
        return file_path.name

    album_dir = rel.dir_or_file
    try:
        rel_parts = file_path.relative_to(album_dir).parts
        if len(rel_parts) > 1:
            return file_path.relative_to(album_dir).as_posix()
    except ValueError:
        pass

    return file_path.name


def resolve_zip_arcname(file_path: Path, album_dir: Path) -> str:
    """Calculates archive entry path. Strips top-level folder if it is a monodisc subfolder (e.g., CD 1)."""
    rel_path = file_path.relative_to(album_dir)
    parts = rel_path.parts

    if len(parts) <= 1:
        return rel_path.as_posix()

    top_level_subdirs: Set[str] = {
        p.relative_to(album_dir).parts[0]
        for p in album_dir.rglob("*")
        if p.is_file() and is_valid_asset(p) and len(p.relative_to(album_dir).parts) > 1
    }

    # Monodisc: Collapse single subfolder (e.g. CD 1/01.flac -> 01.flac)
    if len(top_level_subdirs) == 1:
        return Path(*parts[1:]).as_posix()

    # Multi-disc: Keep subfolder hierarchy (e.g., CD 1/01.flac, CD 2/01.flac)
    return rel_path.as_posix()


def create_clean_zip(
    rel: Release, zip_path: Path, exclude_ext: str, opus_map: Dict[Path, Path]
) -> None:
    """Creates deterministic ZIP archives by fixing timestamps and removing platform metadata."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:

        def add_file_to_zip(src_file: Path, arc_name: str) -> None:
            zinfo = zipfile.ZipInfo(filename=arc_name, date_time=FIXED_ZIP_DATETIME)
            zinfo.external_attr = 0o644 << 16
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zinfo.file_size = src_file.stat().st_size      # lets zipfile pick zip64 when needed
            with open(src_file, "rb") as f, zf.open(zinfo, "w") as dst:
                shutil.copyfileobj(f, dst, 1 << 20)

        if rel.kind == "album" and rel.dir_or_file.is_dir():
            album_dir = rel.dir_or_file
            for file_path in album_dir.rglob("*"):
                if not file_path.is_file() or not is_valid_asset(file_path):
                    continue
                ext = file_path.suffix.lower()
                if ext == exclude_ext:
                    continue

                arc_name = resolve_zip_arcname(file_path, album_dir)

                if exclude_ext == ".flac" and ext == ".opus":
                    add_file_to_zip(file_path, arc_name)
                elif exclude_ext == ".opus" and ext == ".flac":
                    add_file_to_zip(file_path, arc_name)
                elif ext not in (".flac", ".opus"):
                    add_file_to_zip(file_path, arc_name)
        else:
            flac_file = rel.tracks[0].path if rel.tracks else rel.dir_or_file
            if exclude_ext == ".opus":
                add_file_to_zip(flac_file, flac_file.name)
            elif exclude_ext == ".flac" and flac_file in opus_map:
                opus_file = opus_map[flac_file]
                add_file_to_zip(opus_file, opus_file.name)
            if rel.cover_path and rel.cover_path.exists():
                add_file_to_zip(rel.cover_path, rel.cover_path.name)


def delete_local_release(rel: Release) -> None:
    """Safely removes local release files or directory after a successful upload."""
    target = rel.dir_or_file
    if not target or not target.exists():
        return

    try:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink(missing_ok=True)
            target.with_suffix(".opus").unlink(missing_ok=True)
            if rel.cover_path and rel.cover_path.exists():
                rel.cover_path.unlink(missing_ok=True)
    except Exception as e:
        print(f"  ! Warning: Failed to clean up local release target {target}: {e}")
