"""
Run metadata providers concurrently but report them in order.

Each provider's stdout is captured per-thread and replayed, in provider order,
when its result is yielded -- so the log reads exactly like the sequential run.

`first` names providers whose results later providers may depend on (IDs, UPC,
links). They run as wave 1 and are yielded (so the caller merges them into the
Release) BEFORE wave 2 starts. Everything else runs together in wave 2.
"""
from __future__ import annotations

import io
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Iterator, List, Sequence, Tuple

from .. import concurrency

DEFAULT_FIRST = ("Qobuz", "MusicBrainz")


class _ThreadStdout:
    """sys.stdout proxy: threads that called capture() write to a private
    buffer; every other thread (incl. main) passes straight through."""

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def capture(self) -> io.StringIO:
        buf = io.StringIO()
        self._local.buf = buf
        return buf

    def release(self) -> None:
        self._local.buf = None

    def write(self, s):
        buf = getattr(self._local, "buf", None)
        return (buf if buf is not None else self._real).write(s)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run(provider, rel, proxy):
    buf = proxy.capture()
    result, err = None, None
    try:
        result = provider.fetch(rel)
    except Exception as e:  # reported in order by the caller side
        err = e
    finally:
        text = buf.getvalue()
        proxy.release()
    return result, err, text


def _run_group(rel, group: List, proxy) -> Iterator[Tuple[object, dict]]:
    if not group:
        return
    workers = max(1, min(len(group), concurrency.enrich_workers()))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="enrich") as ex:
        futs = [(p, ex.submit(_run, p, rel, proxy)) for p in group]
        for provider, fut in futs:
            result, err, text = fut.result()
            if text:
                sys.stdout.write(text)
            if err is not None:
                print(f"  ! {provider.name} provider failed: {err}")
                continue
            yield provider, result


def iter_results(rel, providers: Sequence, first: Sequence[str] = DEFAULT_FIRST):
    """Yield (provider, result) in provider order; failures are printed and skipped."""
    real = sys.stdout
    proxy = _ThreadStdout(real)
    sys.stdout = proxy
    try:
        head = [p for p in providers if p.name in first]
        tail = [p for p in providers if p.name not in first]
        yield from _run_group(rel, head, proxy)
        yield from _run_group(rel, tail, proxy)
    finally:
        sys.stdout = real
