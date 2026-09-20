"""Ordered, per-file, retrying Internet Archive upload."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, Sequence

import internetarchive as ia

ATTEMPTS = 5      # per file
TIMEOUT = 600     # seconds; ia's default (~120s) is too short for big ZIP PUTs


def _priority(key: str) -> int:
    k = key.lower()
    if k.endswith(".zip"):
        return 3  # heavy bundles last
    if k.endswith(".flac"):
        return 2
    if k.endswith(".opus"):
        return 1
    return 0      # cover, docs, script backup


def upload_ordered(
    identifier: str,
    files: Dict[str, str],
    metadata: dict,
    fatal: Sequence[str] = (),
) -> None:
    """Upload light files first, ZIPs last, one file per request with retries.
    Raises on the first file that still fails after all attempts; a re-run
    skips everything already on IA (checksum=True) and resumes from there."""
    order = sorted(
        files.items(),
        key=lambda kv: (_priority(kv[0]), Path(kv[1]).stat().st_size),
    )
    total = len(order)
    meta_sent = False

    for i, (key, path) in enumerate(order, 1):
        size_mb = Path(path).stat().st_size / (1024 * 1024)
        print(f"\n  [{i}/{total}] {key} ({size_mb:.1f} MB)")

        for attempt in range(1, ATTEMPTS + 1):
            try:
                responses = ia.upload(
                    identifier,
                    files={key: path},
                    metadata=None if meta_sent else metadata,
                    queue_derive=(i == total),  # derive once, after the last file
                    verbose=True,
                    checksum=True,
                    retries=10,
                    retries_sleep=20,
                    request_kwargs={"timeout": TIMEOUT},
                )
                bad = [r for r in responses if getattr(r, "status_code", 200) >= 400]
                if bad:
                    raise RuntimeError(f"HTTP {bad[0].status_code} uploading {key}")
                meta_sent = True
                break
            except Exception as e:
                if attempt == ATTEMPTS or any(f in str(e).lower() for f in fatal):
                    raise
                wait = min(30 * attempt, 180)
                print(f"\n  ! {key}: {e}\n    retry {attempt}/{ATTEMPTS - 1} in {wait}s")
                time.sleep(wait)
