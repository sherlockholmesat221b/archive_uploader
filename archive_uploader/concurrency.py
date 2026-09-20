"""Central worker-count knobs. --aggressive flips them all at once."""
from __future__ import annotations

import os

_AGGRESSIVE = False


def set_aggressive(value: bool) -> None:
    global _AGGRESSIVE
    _AGGRESSIVE = bool(value)


def aggressive() -> bool:
    return _AGGRESSIVE


def cpus() -> int:
    return os.cpu_count() or 4


def opus_workers() -> int:
    """Parallel opusenc processes (each uses one core)."""
    return cpus() if _AGGRESSIVE else max(2, cpus() // 2)


def upload_workers() -> int:
    """Concurrent IA file uploads (after the first file creates the item)."""
    return 4 if _AGGRESSIVE else 1


def enrich_workers() -> int:
    """Concurrent metadata provider lookups."""
    return 8
