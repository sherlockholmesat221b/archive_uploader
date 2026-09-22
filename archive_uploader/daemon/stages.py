"""
The scheduler knows nothing about Qobuz, IA or mega: it only calls these.
Each method runs on a worker thread, may block, and reports bytes with
ctx.progress(n) (or by returning an int). Raise to fail (scheduler retries).

ctx.first        -> (upload only) True if no Part of this release is on IA yet
ctx.event(k, d)  -> free-form event for the stats page, e.g. ctx.event("http_503", url)
ctx.cancelled()  -> poll in long loops
ctx.settings     -> live settings dict
ctx.sch          -> the Scheduler (its .db is the only safe way to read/write job.meta
                     from inside a stage -- stages don't keep state between calls)
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class PartSpec:
    label: str
    est_bytes: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanResult:
    title: str = ""
    parts: List[PartSpec] = field(default_factory=list)
    start_stage: str = "download"      # "opus" for already-local files
    meta: Dict[str, Any] = field(default_factory=dict)
    skip: str = ""                     # non-empty -> job is marked done with this note


class Stages:
    # containers (artist / folder) -> list of {"kind": "album"|"local", "ref": str, "title": str}
    def expand(self, job, ctx) -> List[Dict[str, Any]]: raise NotImplementedError
    # album / local -> PlanResult
    def plan(self, job, ctx) -> PlanResult: raise NotImplementedError
    def download(self, job, part, ctx): raise NotImplementedError
    def opus(self, job, part, ctx): raise NotImplementedError
    def upload(self, job, part, ctx): raise NotImplementedError
    def mega(self, job, part, ctx): raise NotImplementedError
    # once every Part is on IA: final description/metadata update
    def finalize(self, job, ctx): raise NotImplementedError
    # after a Part's last stage: free its disk (must NOT touch user-owned local folders)
    def cleanup_part(self, job, part, ctx): pass
    def cleanup_job(self, job, ctx): pass


class QobuzStages(Stages):
    """
    Real implementation, wired to your existing uploader/packaging/mega_backup code.

    Why upload_release() is safe to call once per Part (not once for the
    whole release):
      - build_ia_payload()/get_expected_file_keys() only ever look at
        rel.tracks whose .path currently exists on disk, so a rel scoped to
        one Part's files produces a manifest scoped to that Part.
      - Zips are release-wide (one "-flac-complete.zip" key for the whole
        album), so they CANNOT be built per-Part without repeatedly
        overwriting that key with a partial zip. The make_zips=False patch
        turns them off for every multi-part call; parts.py already decided
        zip_ok=False for anything over the size cap, so this only matters
        for multipart releases in the first place.
      - CombinedStateStore.mark_uploaded() REPLACES (not merges) its local
        file_manifest rows each call (record_file_manifest does DELETE then
        INSERT), so calling it per-Part leaves local SQLite reflecting only
        the LAST Part uploaded. That's harmless -- IA itself is correct,
        local state just isn't authoritative between Parts -- but it means
        local state alone can never confirm "this release, in full, is
        done". finalize() tracks the *union* of every Part's expected keys
        itself (job.meta["keys"]) and checks IA directly instead.

    Every stage method here is called fresh with no state carried over from
    the previous call (the scheduler doesn't keep Stages instances tied to
    a particular job), so `_rel_for_part` rebuilds the Release from whatever
    is currently staged on disk, and any state that must survive between
    Parts (the resolved IA identifier, the running union of expected keys)
    is persisted into job.meta via ctx.sch.db, not kept on self.
    """

    def __init__(self, state=None, collection: str = "opensource_audio",
                mediatype: str = "audio", opus_bitrate: str = "192k"):
        self.collection, self.mediatype, self.opus_bitrate = collection, mediatype, opus_bitrate
        self._state = state
        self._lock = threading.Lock()

    def _store(self):
        if self._state is None:
            from ..state.combined import CombinedStateStore
            self._state = CombinedStateStore()
        return self._state

    # -------------------------------------------------------------- expand
    def expand(self, job, ctx):
        from ..sources.kabooz_parts import _sess
        from ..scanning import scan_directory
        from pathlib import Path
        if job["kind"] == "artist":
            sess = _sess()
            _, aid = sess.resolve_id(job["ref"], "artist")
            rtype = job["opts"].get("release_type")
            return [{"kind": "album", "ref": str(r.id),
                     "title": getattr(r, "display_title", getattr(r, "title", ""))}
                    for r in sess.iter_releases(aid, release_type=rtype, page_size=100) if r.id]
        rels = scan_directory(Path(job["ref"]))
        return [{"kind": "local", "ref": str(r.dir_or_file), "title": r.title} for r in rels]

    # ---------------------------------------------------------------- plan
    def plan(self, job, ctx):
        from ..sources.kabooz_parts import plan_release
        if job["kind"] != "album":
            raise NotImplementedError("local-release planning: not wired yet")
        q = job["opts"].get("quality")
        album_id, album, plan = plan_release(job["ref"], q)
        upc = getattr(album, "upc", "") or ""
        title = getattr(album, "display_title", album_id)
        store = self._store()
        if any(k and store.is_uploaded(k) for k in (upc, album_id)):
            return PlanResult(title=title, skip="already uploaded")
        parts = [PartSpec(p.label, p.est_bytes, {"track_ids": p.track_ids, "n": p.index})
                 for p in plan.parts]
        return PlanResult(title=title, parts=parts,
                          meta={"album_id": album_id, "upc": upc, "zip_ok": plan.zip_ok,
                                "est_total": plan.est_total, "multipart": plan.multipart,
                                "identifier": "", "keys": []})

    # ------------------------------------------------------------ download
    def download(self, job, part, ctx):
        from ..sources.kabooz_parts import _sess, fetch_part
        from ..parts import Part, TrackRef
        album_id = job["meta"]["album_id"]
        album = _sess().get_album(album_id)
        p = Part(index=part["payload"]["n"], total=part["total"],
                 tracks=[TrackRef(id=i, disc=1, number=0) for i in part["payload"]["track_ids"]])
        _, stage = fetch_part(album_id, album, p, job["opts"].get("quality"))
        return sum(f.stat().st_size for f in stage.rglob("*.flac"))   # measured, not estimated

    # --------------------------------------------------------------- shared
    def _rel_for_part(self, job, part):
        """Rebuild the Release from exactly this Part's own subfolder (never
        the release's shared staging root -- see kabooz_parts.py's module
        docstring for why: other Parts of the same release may be
        downloading, uploading or being cleaned up concurrently). Recomputed
        fresh each stage call rather than passed between stages, since the
        scheduler doesn't keep stage objects alive between calls."""
        from ..sources.kabooz_parts import part_dir
        from ..scanning import scan_directory
        album_id = job["meta"]["album_id"]
        releases = scan_directory(part_dir(album_id, part["idx"]))
        if not releases:
            raise RuntimeError(f"{album_id} part {part['idx']}: no staged files found "
                              "(already cleaned up, or this Part hasn't downloaded yet?)")
        rel = releases[0]
        rel.provider_ids["Qobuz"] = album_id
        rel.upc = rel.upc or job["meta"].get("upc", "")
        ident = job["meta"].get("identifier")
        if ident:
            rel.identifier = ident
        return rel

    def _cache_identifier(self, job, rel, ctx) -> str:
        """resolve_identifier() is deterministic for a given (artist, title,
        id_hash) -- same result every call -- but caching avoids repeating
        its IA/state lookups on every Part, and guarantees every Part's rel
        carries the exact same identifier string, read/written through the
        job row so it survives across Parts (this object doesn't)."""
        ident = job["meta"].get("identifier")
        if ident:
            return ident
        from ..ia.identifiers import resolve_identifier
        base = f"{rel.artist} {rel.title}".strip() or rel.dir_or_file.name
        id_hash = hashlib.md5(base.encode("utf-8")).hexdigest()[:8]
        ident = rel.identifier or resolve_identifier(base, id_hash, self._store())
        with self._lock:
            j = ctx.sch.db.get_job(job["id"])
            meta = dict(j["meta"])
            if not meta.get("identifier"):
                meta["identifier"] = ident
                ctx.sch.db.upd("jobs", job["id"], meta=meta)
        return ident

    def _add_keys(self, job, keys, ctx) -> None:
        with self._lock:
            j = ctx.sch.db.get_job(job["id"])
            meta = dict(j["meta"])
            have = set(meta.get("keys") or [])
            have |= set(keys)
            meta["keys"] = sorted(have)
            ctx.sch.db.upd("jobs", job["id"], meta=meta)

    # ----------------------------------------------------------------- opus
    def opus(self, job, part, ctx):
        from ..opus_cache import derive_opus_file
        rel = self._rel_for_part(job, part)
        total = 0
        for t in rel.tracks:
            if not t.path or not t.path.exists():
                continue
            before = t.path.stat().st_size
            derive_opus_file(t.path, job["opts"].get("opus_bitrate", self.opus_bitrate))
            total += before
            ctx.progress(before)
            if ctx.cancelled():
                break
        return 0   # progress() already reported per-track; avoid double-counting

    # --------------------------------------------------------------- upload
    def upload(self, job, part, ctx):
        from ..ia.uploader import get_expected_file_keys, upload_release
        rel = self._rel_for_part(job, part)
        rel.identifier = self._cache_identifier(job, rel, ctx)
        store = self._store()

        keys = get_expected_file_keys(rel, make_zips=False)
        self._add_keys(job, keys, ctx)

        before = sum(t.path.stat().st_size for t in rel.tracks if t.path and t.path.exists())
        # zip_ok is decided once at plan time (parts.py): True only for a
        # release small enough to be a single Part. A multi-Part release
        # must never build zips per-Part -- see the class docstring.
        make_zips = bool(job["meta"].get("zip_ok", False))
        upload_release(rel, self.collection, self.mediatype, state=store,
                       opus_bitrate=job["opts"].get("opus_bitrate", self.opus_bitrate),
                       make_zips=make_zips)
        ctx.progress(before)     # upload_release() has no progress callback; report as one unit
        return 0

    # ----------------------------------------------------------------- mega
    def mega(self, job, part, ctx):
        from ..mega_backup import mega_backup_release, REMOTE_ROOT
        rel = self._rel_for_part(job, part)
        before = sum(t.path.stat().st_size for t in rel.tracks if t.path and t.path.exists())
        # Only override remote_root if the job explicitly set one -- otherwise
        # use mega_backup.py's own REMOTE_ROOT (I previously hardcoded a
        # fallback here that was missing the "/Root" prefix megatools needs;
        # don't repeat that mistake by guessing a default again).
        ok = mega_backup_release(rel, remote_root=job["opts"].get("mega_root") or REMOTE_ROOT)
        if not ok:
            raise RuntimeError("mega_backup_release() reported failure (see log for the mega.nz error)")
        ctx.progress(before)
        return 0

    # ------------------------------------------------------------- cleanup
    def cleanup_part(self, job, part, ctx):
        from ..sources.kabooz_parts import clear_part
        clear_part(job["meta"]["album_id"], part["idx"])

    def cleanup_job(self, job, ctx):
        from ..sources.kabooz_parts import cleanup_release
        if job["kind"] == "album" and job["meta"].get("album_id"):
            cleanup_release(job["meta"]["album_id"])

    # ------------------------------------------------------------ finalize
    def finalize(self, job, ctx):
        """Runs once, after every Part has uploaded. Confirms IA actually has
        every key any Part expected (the union tracked in job.meta["keys"]),
        then records that full set in local state -- this is the one point
        where local SQLite ends up with the *complete* manifest, since each
        Part's own upload_release() call only ever recorded its own subset."""
        from ..ia.uploader import check_remote_manifest
        identifier = job["meta"].get("identifier")
        keys = set(job["meta"].get("keys") or [])
        if not identifier or not keys:
            raise RuntimeError("finalize: no identifier/keys recorded -- did any Part upload?")
        is_complete, missing = check_remote_manifest(identifier, keys)
        if not is_complete:
            raise RuntimeError(f"finalize: '{identifier}' still missing {len(missing)}/{len(keys)} "
                              f"expected file(s) on IA: {sorted(missing)[:5]}")
        store = self._store()
        if hasattr(store, "mark_uploaded"):
            store.mark_uploaded(identifier=identifier, files=keys,
                               upc=job["meta"].get("upc") or "", qobuz_id=job["meta"]["album_id"])
        ctx.event("finalized", identifier)
