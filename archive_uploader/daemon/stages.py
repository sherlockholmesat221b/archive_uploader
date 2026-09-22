"""
The scheduler knows nothing about Qobuz, IA or mega: it only calls these.
Each method runs on a worker thread, may block, and reports bytes with
ctx.progress(n) (or by returning an int). Raise to fail (scheduler retries).

ctx.first        -> (upload only) True if no Part of this release is on IA yet
ctx.event(k, d)  -> free-form event for the stats page, e.g. ctx.event("http_503", url)
ctx.cancelled()  -> poll in long loops
ctx.settings     -> live settings dict
"""
from __future__ import annotations

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
    """Real implementation. plan/download/expand are done; opus/upload/mega/finalize
    are wired to the existing uploader/packaging/mega_backup code in the next step."""

    def __init__(self, state=None):
        self.state = state          # uploaded-state store, for skip checks

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

    def plan(self, job, ctx):
        from ..sources.kabooz_parts import plan_release
        if job["kind"] != "album":
            raise NotImplementedError("local-release planning: wired in the next step")
        q = job["opts"].get("quality")
        album_id, album, plan = plan_release(job["ref"], q)
        upc = getattr(album, "upc", "") or ""
        title = getattr(album, "display_title", album_id)
        if self.state and any(k and self.state.is_uploaded(k) for k in (upc, album_id)):
            return PlanResult(title=title, skip="already uploaded")
        parts = [PartSpec(p.label, p.est_bytes, {"track_ids": p.track_ids, "n": p.index})
                 for p in plan.parts]
        return PlanResult(title=title, parts=parts,
                          meta={"album_id": album_id, "zip_ok": plan.zip_ok,
                                "est_total": plan.est_total, "multipart": plan.multipart})

    def download(self, job, part, ctx):
        from ..sources.kabooz_parts import _sess, fetch_part
        from ..parts import Part, TrackRef
        album_id = job["meta"]["album_id"]
        album = _sess().get_album(album_id)
        p = Part(index=part["payload"]["n"], total=part["total"],
                 tracks=[TrackRef(id=i, disc=1, number=0) for i in part["payload"]["track_ids"]])
        _, stage = fetch_part(album_id, album, p, job["opts"].get("quality"))
        return sum(f.stat().st_size for f in stage.rglob("*.flac"))   # measured, not estimated
