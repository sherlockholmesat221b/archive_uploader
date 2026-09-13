"""Interactive terminal metadata review for enriched releases.

Uses only Python's standard-library curses module, so it works without adding
another UI dependency (including on Termux builds that provide curses).
"""
from __future__ import annotations

import curses
import textwrap
from typing import Callable, List, Tuple

from .enrichment.overrides import SIMPLE_FIELDS, TRACK_FIELDS, save_review_override
from .models import Release, TrackFile

RELEASE_LABELS = {
    "identifier": "IA identifier",
    "title": "Title",
    "artist": "Artist",
    "date": "Date",
    "genre": "Genre",
    "label": "Label",
    "upc": "UPC / Barcode",
    "isrc": "ISRC",
    "composer": "Composer",
    "copyright": "Copyright",
    "audio_spec": "Audio spec",
    "external_description": "Description",
    "wikipedia_article": "Wikipedia article",
    "cover_url": "Cover URL",
}

REVIEW_FIELDS = ("identifier",) + tuple(f for f in SIMPLE_FIELDS if f != "identifier")


def _edit(stdscr: "curses.window", prompt: str, value: str, max_len: int = 4096) -> str:
    h, w = stdscr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    stdscr.addnstr(h - 2, 0, (prompt + ": ")[: max(1, w - 1)], w - 1)
    stdscr.refresh()
    try:
        raw = stdscr.getstr(h - 1, 0, max_len)
        return raw.decode("utf-8", "replace")
    finally:
        curses.noecho()
        curses.curs_set(0)


def _run_editor(
    stdscr: "curses.window",
    title: str,
    fields: List[Tuple[str, Callable[[], str], Callable[[str], None]]],
) -> str:
    index = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, title, w - 1, curses.A_BOLD)
        stdscr.addnstr(1, 0, "↑/↓ select  Enter edit  S save  A approve+save  Q cancel", w - 1)
        visible = max(1, h - 5)
        start = max(0, min(index - visible // 2, len(fields) - visible))
        for row, (name, getter, _) in enumerate(fields[start : start + visible], start=3):
            i = start + row - 3
            label = RELEASE_LABELS.get(name, name)
            value = getter().replace("\n", " ↵ ")
            if len(value) > max(0, w - 28):
                value = value[: max(0, w - 31)] + "..."
            line = f"{label:<24} {value}"
            stdscr.addnstr(row, 0, line, w - 1, curses.A_REVERSE if i == index else 0)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            index = (index - 1) % len(fields)
        elif key in (curses.KEY_DOWN, ord("j")):
            index = (index + 1) % len(fields)
        elif key in (10, 13, curses.KEY_ENTER):
            name, getter, setter = fields[index]
            new_value = _edit(stdscr, RELEASE_LABELS.get(name, name), getter())
            setter(new_value)
        elif key in (ord("s"), ord("S")):
            return "save"
        elif key in (ord("a"), ord("A")):
            return "approve"
        elif key in (ord("q"), ord("Q"), 27):
            return "cancel"


def _track_editor(stdscr: "curses.window", track: TrackFile) -> str:
    fields = [
        (name, lambda n=name: getattr(track, n), lambda value, n=name: setattr(track, n, value))
        for name in TRACK_FIELDS
    ]
    return _run_editor(stdscr, f"Track: {track.path.name}", fields)


def _main(stdscr: "curses.window", rel: Release) -> str:
    curses.curs_set(0)
    release_fields = [
        (name, lambda n=name: getattr(rel, n), lambda value, n=name: setattr(rel, n, value))
        for name in REVIEW_FIELDS
    ]
    # Track entries are represented separately so the release editor remains
    # usable even for albums with dozens of tracks.
    index = 0
    total = len(release_fields) + len(rel.tracks)
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, f"Metadata review: {rel.artist} — {rel.title}", w - 1, curses.A_BOLD)
        stdscr.addnstr(1, 0, "↑/↓ select  Enter edit  S save  A approve+save/upload  Q quit", w - 1)
        stdscr.addnstr(2, 0, f"Source: {rel.source}    Tracks: {len(rel.tracks)}", w - 1)
        visible = max(1, h - 5)
        start = max(0, min(index - visible // 2, total - visible))
        for row, pos in enumerate(range(start, min(total, start + visible)), start=4):
            if pos < len(release_fields):
                name, getter, _ = release_fields[pos]
                value = getter().replace("\n", " ↵ ")
                label = RELEASE_LABELS.get(name, name)
                if len(value) > max(0, w - 30):
                    value = value[: max(0, w - 33)] + "..."
                line = f"{label:<24} {value}"
            else:
                track = rel.tracks[pos - len(release_fields)]
                line = f"Track {pos - len(release_fields) + 1:>3}: {track.title or track.path.name}"
            stdscr.addnstr(row, 0, line, w - 1, curses.A_REVERSE if pos == index else 0)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            index = (index - 1) % total
        elif key in (curses.KEY_DOWN, ord("j")):
            index = (index + 1) % total
        elif key in (10, 13, curses.KEY_ENTER):
            if index < len(release_fields):
                name, getter, setter = release_fields[index]
                setter(_edit(stdscr, RELEASE_LABELS.get(name, name), getter()))
            else:
                _track_editor(stdscr, rel.tracks[index - len(release_fields)])
        elif key in (ord("s"), ord("S")):
            save_review_override(rel)
            return "save"
        elif key in (ord("a"), ord("A")):
            save_review_override(rel)
            return "approve"
        elif key in (ord("q"), ord("Q"), 27):
            return "cancel"


def review_release(rel: Release) -> str:
    """Review a release; returns ``approve``, ``save`` or ``cancel``."""
    return curses.wrapper(_main, rel)
