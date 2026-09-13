"""
Composes the two state stores instead of picking one:

- LogStateStore (log_store.py): append-only per-device ndjson, synced
  between devices however you like. The only thing that can actually
  answer "has ANY device already uploaded this" — SQLite alone can't,
  since it's local-only and was never synced. See DECISIONS.md.

- SQLiteStateStore (store.py): local-only, but indexed by UPC/qobuz_id
  and able to check per-file manifest completeness, and holds the raw
  provider JSON blobs. The log store deliberately doesn't do any of
  this (see log_store.py's docstring) — it only ever records
  "identifier X, these files, at time T".

Exposes the same method names both callers already use via duck-typing
(hasattr(store, "is_uploaded") / "mark_uploaded" / "record_upload_state"
in ia/identifiers.py and ia/uploader.py), so this is a drop-in
replacement for `Store()` at the two construction sites — no changes
needed to the call sites themselves beyond what constructs the store.

Per DECISIONS.md: neither store is the actual correctness guarantee.
ia/uploader.py's live IA manifest check is. If both stores are wrong,
stale, or missing, the worst case is a redundant upload attempt that
IA's own manifest check then catches — never a silent duplicate.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set, Union

from .log_store import LogStateStore
from .store import SQLiteStateStore


class CombinedStateStore:
    def __init__(
        self,
        sqlite_store: Optional[SQLiteStateStore] = None,
        log_store: Optional[LogStateStore] = None,
    ):
        self.sqlite = sqlite_store or SQLiteStateStore()
        self.log = log_store or LogStateStore()

    def is_uploaded(
        self,
        key_or_id: str,
        expected_files: Optional[Iterable[str]] = None,
    ) -> bool:
        # SQLite first: it's the only one that can match by UPC/qobuz_id
        # and verify every expected file was recorded, not just the
        # identifier itself.
        if self.sqlite.is_uploaded(key_or_id, expected_files=expected_files):
            return True

        # Cross-device fallback. The log only ever stores an identifier +
        # its file list per "uploaded" event — it can't be matched by
        # UPC/qobuz_id (key_or_id might be either), so this only helps
        # when key_or_id is actually an IA identifier. That's fine: the
        # live IA manifest check in ia/uploader.py is the real backstop
        # regardless, per DECISIONS.md.
        if not key_or_id:
            return False
        return self.log.is_uploaded(key_or_id)

    def mark_uploaded(
        self,
        identifier: str,
        files: Iterable[str],
        metadata: Optional[Dict[str, Any]] = None,
        upc: Optional[str] = None,
        qobuz_id: Optional[str] = None,
        status: str = "completed",
    ) -> None:
        files = list(files)
        self.sqlite.mark_uploaded(
            identifier=identifier, files=files, metadata=metadata, upc=upc, qobuz_id=qobuz_id, status=status
        )
        if status == "completed":
            # Only "completed" events go in the log — it's a small
            # append-only file meant to answer "is this done", not a
            # general state table (that's what SQLite is for).
            self.log.mark_uploaded(identifier, files)

    def record_upload_state(self, *args, **kwargs) -> None:
        # in_progress/failed/taken_offline bookkeeping (raw provider JSON,
        # ia_payload, etc.) stays SQLite-only by design — see log_store.py's
        # docstring on why it's kept minimal.
        return self.sqlite.record_upload_state(*args, **kwargs)

    @staticmethod
    def extract_file_metadata(file_path: Path) -> dict:
        return SQLiteStateStore.extract_file_metadata(file_path)

    def all_uploaded_identifiers(self) -> Set[str]:
        """Union of every identifier any device's log has recorded as uploaded."""
        return self.log.all_uploaded()
