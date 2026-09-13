"""Optional mega.nz backup of a release's original local files, via the
megatools CLI (https://megatools.megous.com/).

Deliberately independent of the Internet Archive upload path: this
preserves the release's original files exactly as they sit on disk —
before Opus derivation, ZIP packaging, or --delete-after-upload touches
them — as a second, off-IA copy. Meant for rare items worth two homes.

Auth via ~/.config/archive_uploader/secrets.json:
    {"mega": {"email": "you@example.com", "password": "..."}}
or the ARCHIVE_UPLOADER_MEGA_EMAIL / ARCHIVE_UPLOADER_MEGA_PASSWORD
environment variables (see config.get_secret). With neither set,
mega_backup_release() prints a warning and does nothing — it never
raises, so a missing/failed backup can't take down the IA upload that
follows it.

Requires `megatools` (megaput, megamkdir, megacopy) on PATH:
    apt install megatools   /   brew install megatools   /   build from
    https://megatools.megous.com/
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List

from .config import get_secret
from .models import Release
from .textutils import slugify
from .ui import info

REMOTE_ROOT = "/Root/archive_uploader_backups"


def _mega_auth_args() -> List[str]:
    email = get_secret("mega", "email")
    password = get_secret("mega", "password")
    if not email or not password:
        return []
    return ["--username", email, "--password", password]


def mega_backup_release(rel: Release, remote_root: str = REMOTE_ROOT) -> bool:
    """Uploads rel's original local files to mega.nz, preserving folder
    structure for albums (via megacopy) or uploading the file(+cover)
    directly for singles (via megaput). Returns True on reported
    success. Never raises — call this and move on either way."""
    target = rel.dir_or_file
    if not target or not target.exists():
        return False

    if not shutil.which("megaput") or not shutil.which("megamkdir"):
        print("  ! megatools not found on PATH — skipping mega.nz backup "
              "(install from https://megatools.megous.com/)")
        return False

    auth = _mega_auth_args()
    if not auth:
        print("  ! No mega.nz credentials configured (see mega_backup.py docstring) — skipping backup.")
        return False

    base = f"{rel.artist} {rel.title}".strip() or target.name
    remote_root = "/" + remote_root.strip("/")
    remote_dir = f"{remote_root}/{slugify(base)}"

    print(f"  ☁️  Backing up to mega.nz: {remote_dir}")
    try:
        # megamkdir does not reliably create missing parent directories.
        # Create each component from the root down and show what happens.
        info("Checking/creating remote directory tree")
        remote_parts = [part for part in remote_dir.split("/") if part]
        current = ""
        for part in remote_parts:
            current += "/" + part
            result = subprocess.run(
                ["megamkdir", *auth, "--no-ask-password", current],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                info(f"✓ Remote directory ready: {current}", 4)
            else:
                error = (result.stderr or result.stdout).strip()
                # megamkdir commonly reports an existing directory as an
                # error.  Keep that non-fatal, but surface any other error.
                if "already exists" in error.lower() or "exists" in error.lower():
                    info(f"· Remote directory already exists: {current}", 4)
                else:
                    print(f"  ! Could not create/check {current}: {error or 'unknown megamkdir error'}")
                    return False

        if target.is_dir():
            if not shutil.which("megacopy"):
                print("  ! megacopy not found on PATH — cannot back up a folder without it.")
                return False
            files = [p for p in target.rglob("*") if p.is_file()]
            total_bytes = sum(p.stat().st_size for p in files)
            info(f"Local release: {len(files)} files, {total_bytes / (1024 * 1024):.2f} MiB", 2)
            info("Uploading with megacopy; native transfer progress follows:", 2)
            result = subprocess.run(
                ["megacopy", *auth, "--no-ask-password",
                 "--local", str(target), "--remote", remote_dir],
            )
        else:
            if not shutil.which("megaput"):
                print("  ! megaput not found on PATH — cannot upload a single file.")
                return False
            files = [str(target)]
            if rel.cover_path and rel.cover_path.exists():
                files.append(str(rel.cover_path))
            info(f"Uploading {len(files)} file(s) with megaput; native transfer progress follows:", 2)
            result = subprocess.run(
                ["megaput", *auth, "--no-ask-password",
                 "--path", remote_dir + "/", *files],
            )

        if result.returncode != 0:
            print(f"  ! mega.nz backup failed (exit {result.returncode})")
            return False

        print("  ✓ mega.nz backup complete.")
        return True
    except Exception as e:
        print(f"  ! mega.nz backup error: {e}")
        return False
