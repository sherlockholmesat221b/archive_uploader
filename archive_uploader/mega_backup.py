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

Multiple accounts: name additional accounts under "accounts" in the same
secrets.json and pass account="name" to mega_backup_release()/
mega_export_link() to use one instead of the default:
    {"mega": {"email": "...", "password": "...",
               "accounts": {"second": {"email": "...", "password": "..."}}}}
or ARCHIVE_UPLOADER_MEGA_SECOND_EMAIL / ..._PASSWORD per named account.
The unnamed default account above still works exactly as before.

Requires `megatools` (megaput, megamkdir, megacopy) on PATH:
    apt install megatools   /   brew install megatools   /   build from
    https://megatools.megous.com/
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List

import json
import os
from typing import Optional, Tuple

from .config import SECRETS_FILE, get_secret
from .models import Release
from .textutils import slugify
from .ui import info

REMOTE_ROOT = "/Root/archive_uploader_backups"


def _mega_creds(account: Optional[str] = None) -> Tuple[str, str]:
    """(email, password) for the default account, or a named one from
    secrets.json's mega.accounts.<account> / ARCHIVE_UPLOADER_MEGA_<ACCOUNT>_*."""
    if not account:
        return get_secret("mega", "email"), get_secret("mega", "password")

    env_e = os.environ.get(f"ARCHIVE_UPLOADER_MEGA_{account.upper()}_EMAIL")
    env_p = os.environ.get(f"ARCHIVE_UPLOADER_MEGA_{account.upper()}_PASSWORD")
    if env_e and env_p:
        return env_e, env_p

    if SECRETS_FILE.exists():
        try:
            data = json.loads(SECRETS_FILE.read_text())
            acc = data.get("mega", {}).get("accounts", {}).get(account, {})
            if acc.get("email") and acc.get("password"):
                return acc["email"], acc["password"]
        except (json.JSONDecodeError, OSError):
            pass
    return "", ""


def _mega_auth_args(account: Optional[str] = None) -> List[str]:
    email, password = _mega_creds(account)
    if not email or not password:
        return []
    return ["--username", email, "--password", password]


def remote_dir_for(rel: Release, remote_root: str = REMOTE_ROOT) -> str:
    """The exact remote path mega_backup_release() would use for this
    release -- factored out so callers (link export, the retroactive
    linking script) can compute it independently without duplicating the
    slug logic or needing a live upload to happen first."""
    base = f"{rel.artist} {rel.title}".strip() or rel.dir_or_file.name
    return f"{'/' + remote_root.strip('/')}/{slugify(base)}"


def mega_export_link(remote_dir: str, account: Optional[str] = None) -> str:
    """Creates (or re-fetches, megaexport is idempotent) a public share link
    for an existing remote folder. Returns "" on any failure -- treat a
    missing link as "couldn't get one this time", never fatal.

    NOTE: megaexport's exact stdout format is parsed leniently (first
    http(s):// token on any line) rather than matched exactly, since this
    hasn't been verified against a real run yet -- check the first real
    call's printed output against what comes back."""
    auth = _mega_auth_args(account)
    if not auth:
        return ""
    if not shutil.which("megaexport"):
        print("  ! megaexport not found on PATH -- can't create a shareable link.")
        return ""
    try:
        result = subprocess.run(
            ["megaexport", *auth, "--no-ask-password", "--create", remote_dir],
            capture_output=True, text=True, timeout=60,
        )
        out = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0 and "http" not in out:
            print(f"  ! Could not create mega share link for {remote_dir}: {out.strip()}")
            return ""
        for line in out.splitlines():
            for tok in line.split():
                if tok.startswith("http"):
                    return tok.strip()
        print(f"  ! megaexport ran but no link found in its output: {out.strip()!r}")
        return ""
    except Exception as e:
        print(f"  ! mega export link failed: {e}")
        return ""


def mega_backup_release(rel: Release, remote_root: str = REMOTE_ROOT,
                        account: Optional[str] = None) -> bool:
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

    auth = _mega_auth_args(account)
    if not auth:
        print("  ! No mega.nz credentials configured (see mega_backup.py docstring) — skipping backup.")
        return False

    remote_dir = remote_dir_for(rel, remote_root)

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
                capture_output=True, text=True,
            )
            print(result.stdout, end="")
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
                capture_output=True, text=True,
            )
            print(result.stdout, end="")

        if result.returncode != 0:
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            out_lines = [l for l in combined.splitlines() if "ERROR:" in l]
            # megacopy has no "skip if already uploaded" mode -- it hard-errors
            # on any pre-existing remote file. A retry after any real progress
            # (or a second manual run) will ALWAYS hit this for every file
            # that made it up last time, so treat "already exists" as the
            # success it actually represents rather than a failure to retry.
            if out_lines and all("already exists" in l.lower() for l in out_lines):
                info(f"· {len(out_lines)} file(s) already on mega.nz from an earlier attempt "
                    "-- treating backup as complete.", 2)
            else:
                err = (result.stderr or "").strip()
                print(f"  ! mega.nz backup failed (exit {result.returncode}){': ' + err if err else ''}")
                return False

        print("  ✓ mega.nz backup complete.")
        return True
    except Exception as e:
        print(f"  ! mega.nz backup error: {e}")
        return False
