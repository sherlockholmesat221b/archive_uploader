"""
Part planning: split one release into Parts that are downloaded, processed,
uploaded and deleted one at a time. One release is ALWAYS one IA item; Parts
only decide how much sits on local disk at once.

Rules
  - total <= part_cap            -> single Part (whole release at once)
  - total >  part_cap            -> Parts on disc boundaries; a disc bigger
                                    than part_cap is split by consecutive tracks
  - zips only when total <= zip_limit (zips need every file at once, so a
    multi-Part release can never have them)

Sizes are ESTIMATED from duration + quality before anything is downloaded.
Pure logic: no network, no IO. Feed it TrackRef objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import groupby
from typing import Dict, List, Sequence

GB = 1024 ** 3
PART_CAP = 10 * GB     # max staged on disk per Part
ZIP_LIMIT = 4 * GB     # zips only for releases at or below this


def bytes_per_sec(bit_depth: int = 16, sample_rate: int = 44100, channels: int = 2) -> int:
    """Rough FLAC size: raw PCM rate * ~0.6 compression. CD ~106 KB/s, 24/96 ~346 KB/s."""
    return int(bit_depth * sample_rate * channels * 0.6 / 8)


@dataclass
class TrackRef:
    id: str
    disc: int
    number: int
    title: str = ""
    duration: float = 0.0          # seconds
    est_bytes: int = 0

    @property
    def sort_key(self):
        return (self.disc, self.number)


@dataclass
class Part:
    index: int                     # 1-based
    total: int = 0                 # filled in by plan_parts
    tracks: List[TrackRef] = field(default_factory=list)

    @property
    def est_bytes(self) -> int:
        return sum(t.est_bytes for t in self.tracks)

    @property
    def discs(self) -> List[int]:
        return sorted({t.disc for t in self.tracks})

    @property
    def label(self) -> str:
        d = self.discs
        span = f"Disc {d[0]}" if len(d) == 1 else f"Discs {d[0]}-{d[-1]}"
        return f"Part {self.index}/{self.total} ({span})"

    @property
    def track_ids(self) -> List[str]:
        return [t.id for t in self.tracks]


@dataclass
class Plan:
    parts: List[Part]
    est_total: int
    zip_ok: bool

    @property
    def multipart(self) -> bool:
        return len(self.parts) > 1


def track_refs_from_qobuz(album: Dict, bit_depth: int = 16, sample_rate: int = 44100) -> List[TrackRef]:
    """Build TrackRefs from raw Qobuz album JSON (album['tracks']['items'])."""
    bps = bytes_per_sec(bit_depth, sample_rate)
    out: List[TrackRef] = []
    for t in (album.get("tracks") or {}).get("items", []):
        dur = float(t.get("duration") or 0)
        out.append(TrackRef(
            id=str(t["id"]),
            disc=int(t.get("media_number") or 1),
            number=int(t.get("track_number") or 0),
            title=t.get("title", ""),
            duration=dur,
            est_bytes=int(dur * bps),
        ))
    return out


def plan_parts(
    tracks: Sequence[TrackRef],
    part_cap: int = PART_CAP,
    zip_limit: int = ZIP_LIMIT,
    merge_small_discs: bool = True,
) -> Plan:
    tracks = sorted(tracks, key=lambda t: t.sort_key)
    total = sum(t.est_bytes for t in tracks)

    if total <= part_cap:
        p = Part(index=1, total=1, tracks=list(tracks))
        return Plan([p], total, zip_ok=total <= zip_limit)

    parts: List[Part] = []
    cur: List[TrackRef] = []
    cur_b = 0

    def flush():
        nonlocal cur, cur_b
        if cur:
            parts.append(Part(index=len(parts) + 1, tracks=cur))
        cur, cur_b = [], 0

    for _, grp in groupby(tracks, key=lambda t: t.disc):
        disc = list(grp)
        d_bytes = sum(t.est_bytes for t in disc)

        # keep disc boundaries: close the current Part if this disc won't fit,
        # or always close it when not merging small discs
        if cur and (not merge_small_discs or cur_b + d_bytes > part_cap):
            flush()

        if d_bytes <= part_cap:
            cur.extend(disc)
            cur_b += d_bytes
        else:  # one disc alone exceeds the cap -> split by consecutive tracks
            for t in disc:
                if cur and cur_b + t.est_bytes > part_cap:
                    flush()
                cur.append(t)
                cur_b += t.est_bytes
    flush()

    for p in parts:
        p.total = len(parts)
    return Plan(parts, total, zip_ok=False)


# ---------------------------------------------------------------------------
# kabooz Album adapter (getattr-based so a missing field degrades, not crashes)
# ---------------------------------------------------------------------------

def spec_for_quality(quality_name: str, album_bit_depth=None, album_rate=None):
    """(bit_depth, sample_rate_hz) actually delivered for a requested quality."""
    rate = float(album_rate or 0)
    if 0 < rate < 1000:            # Qobuz reports kHz (44.1 / 96 / 192)
        rate *= 1000
    q = (quality_name or "").upper()
    if q in ("FLAC_16", "CD", "LOSSLESS", "FLAC"):
        return 16, 44100
    if q in ("FLAC_24_96", "24BIT", "24_96"):
        return 24, int(min(rate or 96000, 96000))
    return int(album_bit_depth or 24), int(rate or 192000)   # HI_RES / best


def track_refs_from_album(album, quality_name: str = "FLAC_16") -> List[TrackRef]:
    """Build TrackRefs from a kabooz Album (album.tracks.items)."""
    bd, sr = spec_for_quality(
        quality_name,
        getattr(album, "maximum_bit_depth", None),
        getattr(album, "maximum_sampling_rate", None),
    )
    bps = bytes_per_sec(bd, sr)
    items = getattr(getattr(album, "tracks", None), "items", None) or []
    out: List[TrackRef] = []
    for t in items:
        dur = float(getattr(t, "duration", 0) or 0)
        out.append(TrackRef(
            id=str(t.id),
            disc=int(getattr(t, "media_number", 1) or 1),
            number=int(getattr(t, "track_number", 0) or 0),
            title=getattr(t, "display_title", getattr(t, "title", "")),
            duration=dur,
            est_bytes=int(dur * bps),
        ))
    return out
