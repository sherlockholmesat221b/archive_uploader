"""
Persistent, verified Opus derivation.

Opus files stay next to their FLACs. A hidden per-folder manifest
(.opus_manifest.json -- dot-prefixed, so payload/ZIP filters ignore it)
records, per FLAC: source size/mtime/MD5, bitrate, and the Opus size/SHA-256.
A cached Opus is reused only if ALL of these still check out; otherwise it is
re-encoded. Encodes go to a .part file and are renamed atomically, so a killed
opusenc can never leave a truncated file that later looks valid.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

MANIFEST_NAME = ".opus_manifest.json"
_CHUNK = 1 << 20


def _hash_file(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), "utf-8")
    os.replace(tmp, path)


def derive_opus_file(flac_path: Path, bitrate: str = "192k") -> Path:
    """Return a verified Opus for `flac_path`, re-encoding only if needed."""
    opus_path = flac_path.with_suffix(".opus")
    mpath = flac_path.parent / MANIFEST_NAME
    manifest = _load(mpath)
    entry = manifest.get(flac_path.name, {})
    st = flac_path.stat()

    # ---- cache hit? every check must pass -------------------------------
    if entry.get("bitrate") == bitrate and opus_path.is_file():
        flac_same = entry.get("flac_size") == st.st_size and (
            entry.get("flac_mtime_ns") == st.st_mtime_ns
            or entry.get("flac_md5") == _hash_file(flac_path, "md5")
        )
        if (
            flac_same
            and opus_path.stat().st_size == entry.get("opus_size")
            and _hash_file(opus_path, "sha256") == entry.get("opus_sha256")
        ):
            return opus_path
        print(f"  ! Cached Opus failed verification, re-encoding: {opus_path.name}")

    # ---- (re)encode ------------------------------------------------------
    if not shutil.which("opusenc"):
        raise RuntimeError("opusenc command-line tool not found in PATH.")

    flac_md5 = _hash_file(flac_path, "md5")
    serial_num = int(flac_md5[:8], 16) & 0xFFFFFFFF  # deterministic, as before
    tmp = opus_path.with_name(opus_path.name + ".part")
    tmp.unlink(missing_ok=True)

    cmd = [
        "opusenc", "--quiet",
        "--bitrate", bitrate.replace("k", ""),
        "--serial", str(serial_num),
        str(flac_path), str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise RuntimeError("opusenc produced no output")
        os.replace(tmp, opus_path)
    except subprocess.CalledProcessError as e:
        print(f"  ! opusenc Error for {flac_path.name}:\n{e.stderr}")
        raise
    finally:
        tmp.unlink(missing_ok=True)

    manifest[flac_path.name] = {
        "bitrate": bitrate,
        "flac_size": st.st_size,
        "flac_mtime_ns": st.st_mtime_ns,
        "flac_md5": flac_md5,
        "opus_size": opus_path.stat().st_size,
        "opus_sha256": _hash_file(opus_path, "sha256"),
    }
    _save(mpath, manifest)
    return opus_path
