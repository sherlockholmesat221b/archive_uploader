"""Ordered, paced, rate-limit-aware Internet Archive upload (serial or parallel)."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

import internetarchive as ia

from .. import concurrency

ATTEMPTS = 5             # per file
TIMEOUT = 600            # seconds; ia's default (~120s) is too short for big ZIP PUTs
MIN_INTERVAL = 2.0       # seconds between request starts (all threads combined)
RATE_WAITS = (90, 240, 600, 900, 1200)   # seconds to back off on IA throttling

_RATE_SIGNS = (
    "reduce your request rate", "slow down", "slowdown", "too many requests",
    "429", "503", "appears to be spam", "rate limit",
)

_print_lock = threading.Lock()
_pace_lock = threading.Lock()
_next_slot = 0.0          # earliest time the next request may start
_cooldown_until = 0.0     # global pause after a throttle response (shared by all workers)


def _say(msg: str) -> None:
    with _print_lock:
        print(msg)


def _priority(key: str) -> int:
    k = key.lower()
    if k.endswith(".zip"):
        return 3  # heavy bundles last
    if k.endswith(".flac"):
        return 2
    if k.endswith(".opus"):
        return 1
    return 0      # cover, docs, script backup


def _pace() -> None:
    """Global spacing between request starts + honor any shared cooldown."""
    global _next_slot
    while True:
        with _pace_lock:
            now = time.time()
            start = max(now, _next_slot, _cooldown_until)
            if start <= now:
                _next_slot = now + MIN_INTERVAL
                return
            wait = start - now
        time.sleep(min(wait, 5))


def _is_rate_limited(err: Exception) -> bool:
    msg = str(err).lower()
    return any(s in msg for s in _RATE_SIGNS)


def _upload_one(identifier, key, path, metadata, fatal, verbose) -> float:
    """Upload one file. Backs off long on throttling, short on network errors."""
    global _cooldown_until
    t0 = time.time()
    rate_hits = 0
    for attempt in range(1, ATTEMPTS + 1):
        _pace()
        try:
            responses = ia.upload(
                identifier,
                files={key: path},
                metadata=metadata,
                queue_derive=False,  # one derive is queued after everything lands
                verbose=verbose,
                checksum=True,
                retries=2,           # ia's own retry is kept small; WE control backoff
                retries_sleep=30,
                request_kwargs={"timeout": TIMEOUT},
            )
            bad = [r for r in responses if getattr(r, "status_code", 200) >= 400]
            if bad:
                raise RuntimeError(f"HTTP {bad[0].status_code} uploading {key}")
            return time.time() - t0
        except Exception as e:
            if any(f in str(e).lower() for f in fatal):
                raise
            if _is_rate_limited(e):
                if rate_hits >= len(RATE_WAITS):
                    raise RuntimeError(
                        "Internet Archive is throttling this account/item. Stop, wait a few "
                        "hours, then re-run (finished files are skipped). If it persists, "
                        "email info@archive.org with the full error message."
                    ) from e
                wait = RATE_WAITS[rate_hits]
                rate_hits += 1
                with _pace_lock:  # pause EVERY worker, not just this one
                    _cooldown_until = max(_cooldown_until, time.time() + wait)
                _say(f"\n  ! IA throttled us on {Path(key).name}; pausing all uploads {wait // 60} min "
                     f"({rate_hits}/{len(RATE_WAITS)})")
                continue
            if attempt == ATTEMPTS:
                raise
            wait = min(30 * attempt, 180)
            _say(f"\n  ! {Path(key).name}: {e}\n    retry {attempt}/{ATTEMPTS - 1} in {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"gave up on {key}")


def upload_ordered(
    identifier: str,
    files: Dict[str, str],
    metadata: dict,
    fatal: Sequence[str] = (),
    on_file_done: Optional[Callable[[str, int, float], None]] = None,
) -> None:
    """Light files first, ZIPs last. The first file goes alone (it creates the
    item and carries the metadata); the rest go serially, or a few at a time
    with --aggressive. Raises on the first file that still fails; a re-run skips
    whatever is already on IA (checksum=True).

    on_file_done(key, size_bytes, seconds), if given, is called after each
    file actually finishes uploading -- real per-file granularity (this is
    genuinely as fine-grained as upload_ordered's own accounting gets; it
    has no visibility into bytes sent mid-file)."""
    order = sorted(
        files.items(),
        key=lambda kv: (_priority(kv[0]), Path(kv[1]).stat().st_size),
    )
    total = len(order)
    workers = concurrency.upload_workers()
    parallel = workers > 1 and total > 2
    counter = {"n": 0}

    def run(key, path, meta):
        size_bytes = Path(path).stat().st_size
        size_mb = size_bytes / (1024 * 1024)
        if not parallel:
            _say(f"\n  [{counter['n'] + 1}/{total}] {key} ({size_mb:.1f} MB)")
        secs = _upload_one(identifier, key, path, meta, fatal, verbose=not parallel)
        with _print_lock:
            counter["n"] += 1
            if parallel:
                print(f"  \u2713 [{counter['n']}/{total}] {key} ({size_mb:.1f} MB, {secs:.0f}s)")
        if on_file_done:
            on_file_done(key, size_bytes, secs)

    run(*order[0], metadata)  # creates the item + metadata
    rest = order[1:]

    if not parallel:
        for key, path in rest:
            run(key, path, None)
    else:
        print(f"  \u2192 Uploading remaining {len(rest)} file(s), {workers} at a time")
        ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="upload")
        try:
            futs = [ex.submit(run, key, path, None) for key, path in rest]
            for f in futs:
                f.result()
        except BaseException:
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            ex.shutdown(wait=True)

    try:  # queue IA's derive once, after every file has landed
        ia.get_item(identifier).derive()
    except Exception as e:
        _say(f"  ! Could not queue IA derive (files are uploaded fine): {e}")
