"""Internet Archive execution workflow and retry loop."""

import hashlib
from pathlib import Path
from typing import Optional, Set
import internetarchive as ia

from archive_uploader.ia.identifiers import resolve_identifier
from archive_uploader.ia.payload import build_ia_payload, is_valid_payload_file
from archive_uploader.models import Release
from archive_uploader.packaging import delete_local_release, determine_file_key
from archive_uploader.state.combined import CombinedStateStore
from archive_uploader.textutils import slugify
from archive_uploader.ia.transfer import upload_ordered

FATAL_UPLOAD_ERRORS = (
    "access denied", "taken offline", "403", "forbidden",
    "unauthorized", "item is locked", "darked",
)


def get_expected_file_keys(rel: Release, make_zips: bool = True) -> Set[str]:
    """Calculates full expected remote file manifest (every FLAC, every Opus, and ZIPs)."""
    expected = set()

    flac_list = [t.path for t in rel.tracks if t.path and t.path.exists()]
    if not flac_list and rel.dir_or_file.is_file():
        flac_list = [rel.dir_or_file]
    elif not flac_list and rel.dir_or_file.is_dir():
        flac_list = sorted([
            p for p in rel.dir_or_file.rglob("*.flac")
            if is_valid_payload_file(p)
        ])

    for flac_p in flac_list:
        flac_key = determine_file_key(flac_p, rel)
        expected.add(flac_key)

        # Explicitly require derived Opus audio key for EVERY FLAC
        opus_key = str(Path(flac_key).with_suffix(".opus"))
        expected.add(opus_key)

    is_single = (
        rel.kind == "single"
        or getattr(rel, "is_single", False)
        or "(SINGLE)" in rel.title.upper()
        or " - SINGLE" in rel.title.upper()
        or len(flac_list) <= 1
    )

    if not is_single and make_zips:
        base = f"{rel.artist} {rel.title}".strip() or rel.dir_or_file.name
        slug_name = slugify(base)
        expected.add(f"{slug_name}-flac-complete.zip")
        expected.add(f"{slug_name}-opus-complete.zip")

    if rel.cover_path and rel.cover_path.exists():
        ext = rel.cover_path.suffix.lower()
        expected.add(f"cover{ext}")

    return expected


def check_remote_manifest(item_id: str, expected_keys: Set[str]) -> tuple[bool, Set[str]]:
    """Checks live IA item file list against expected manifest."""
    try:
        item = ia.get_item(item_id)
        if not getattr(item, "exists", False):
            return False, expected_keys

        remote_files = {f.name for f in item.get_files()}
        missing = expected_keys - remote_files
        return len(missing) == 0, missing
    except Exception:
        return False, expected_keys


def check_upc_match(rel: Release, expected_keys: Set[str]) -> Optional[str]:
    """Searches IA for UPC match and checks ownership/manifest completeness."""
    if not rel.upc:
        return None

    try:
        results = list(ia.search_items(f'"{rel.upc}"'))
        for res in results:
            target_id = res.get("identifier")
            if not target_id:
                continue

            item = ia.get_item(target_id)
            meta = getattr(item, "metadata", {})

            remote_upc = meta.get("upc") or meta.get("barcode")
            remote_creator = str(meta.get("creator", "")).lower()
            artist_match = rel.artist.lower() in remote_creator

            if remote_upc == rel.upc or artist_match:
                is_complete, missing = check_remote_manifest(target_id, expected_keys)
                if is_complete:
                    return target_id
                else:
                    print(f"  -> Found UPC match '{target_id}', missing {len(missing)} file(s). Resuming...")
    except Exception:
        pass

    return None


def upload_release(
    rel: Release,
    collection: str,
    mediatype: str,
    dry_run: bool = False,
    delete_after: bool = False,
    state: Optional[CombinedStateStore] = None,
    opus_bitrate: str = "192k",
    make_zips: bool = True,
) -> None:
    """Executes pre-checks against local state (SQLite + cross-device log) and live IA manifests before processing."""
    store = state or CombinedStateStore()

    base = f"{rel.artist} {rel.title}".strip() or rel.dir_or_file.name
    id_hash = hashlib.md5(base.encode("utf-8")).hexdigest()[:8]
    identifier = rel.identifier or resolve_identifier(base, id_hash, store)
    expected_keys = get_expected_file_keys(rel, make_zips=make_zips)
    qobuz_id = rel.provider_ids.get("Qobuz", "")

    print(f"  → IA target: {identifier}")
    print(f"  → Expected manifest: {len(expected_keys)} files")

    # 1. Local State Validation (SQLite + cross-device log union)
    if (
        (rel.upc and store.is_uploaded(rel.upc, expected_files=expected_keys))
        or (qobuz_id and store.is_uploaded(qobuz_id, expected_files=expected_keys))
        or store.is_uploaded(identifier, expected_files=expected_keys)
    ):
        print(f"  -> ⏭️ [SKIPPED] '{identifier}' fully complete in local state.")
        if delete_after and not dry_run:
            delete_local_release(rel)
        return

    # 2. Remote IA Manifest Validation (Direct Identifier)
    print("  → Checking Internet Archive manifest")
    is_complete, missing_keys = check_remote_manifest(identifier, expected_keys)
    if is_complete:
        print(f"  -> ⏭️ [SKIPPED] '{identifier}' already fully uploaded on Internet Archive.")
        if hasattr(store, "mark_uploaded"):
            store.mark_uploaded(
                identifier=identifier,
                files=expected_keys,
                upc=rel.upc,
                qobuz_id=qobuz_id,
            )
        if delete_after and not dry_run:
            delete_local_release(rel)
        return
    elif len(missing_keys) < len(expected_keys):
        print(f"  -> [RESUMING] '{identifier}' exists on IA but is missing {len(missing_keys)} file(s) (e.g., Opus audio or ZIPs).")

    # 3. Remote IA Manifest Validation (UPC Match)
    if len(missing_keys) == len(expected_keys):
        existing_upc_id = check_upc_match(rel, expected_keys)
        if existing_upc_id:
            print(f"  -> ⏭️ [SKIPPED] Matched via UPC to fully uploaded item '{existing_upc_id}'.")
            if hasattr(store, "mark_uploaded"):
                store.mark_uploaded(
                    identifier=existing_upc_id,
                    files=expected_keys,
                    upc=rel.upc,
                    qobuz_id=qobuz_id,
                )
            if delete_after and not dry_run:
                delete_local_release(rel)
            return

    # 4. Generate Payload & Run Upload (Filtered to missing files if resuming)
    print("  → Building IA payload, deriving Opus, and packaging archives")
    identifier, metadata, files_dict, temp_cleanup_files = build_ia_payload(
        rel, collection, mediatype, known_identifiers=store, opus_bitrate=opus_bitrate,
        identifier=identifier, make_zips=make_zips,
    )

    # Filter out files that already exist on IA
    if len(missing_keys) < len(expected_keys):
        files_dict = {k: v for k, v in files_dict.items() if k in missing_keys}

    # Print Post-Packaging Metadata Summary
    total_bytes = sum(
        Path(p).stat().st_size for p in files_dict.values() if Path(p).exists()
    )
    total_mb = total_bytes / (1024 * 1024)

    print(f"\n🚀 Ready to Upload: {rel.title}")
    print(f"  • Identifier: {identifier}")
    print(f"  • Collection: {collection}")
    print(f"  • Files to Upload: {len(files_dict)}")
    print(f"  • Total Payload Size: ~{total_mb:.2f} MB\n")

    try:
        if dry_run:
            print(f"  [DRY RUN] Prepared upload payload for '{identifier}' ({len(files_dict)} files to upload).")
            return

        if store and hasattr(store, "record_upload_state"):
            store.record_upload_state(
                identifier=identifier,
                upc=rel.upc or "",
                qobuz_id=qobuz_id or "",
                artist=rel.artist,
                title=rel.title,
                status="in_progress",
                # Previously getattr(rel, "qobuz_raw_data", ...) / "mb_raw_data" —
                # those attributes never existed on Release, so these were
                # always None and qobuz_raw_json/mb_raw_json in SQLite were
                # never actually populated. rel.raw_provider_data is the real
                # thing now (see enrichment/pipeline.py), keyed by each
                # provider's .name.
                qobuz_raw=rel.raw_provider_data.get("Qobuz"),
                mb_raw=rel.raw_provider_data.get("MusicBrainz"),
                wiki_raw=rel.raw_provider_data.get("Wikipedia") or rel.wikipedia_article,
                ia_payload=metadata,
            )

        print(f"  → Uploading {len(files_dict)} file(s) to Internet Archive")
        upload_ordered(identifier, files_dict, metadata, fatal=FATAL_UPLOAD_ERRORS)

        print("   ✓ Upload complete.")
        if hasattr(store, "mark_uploaded"):
            store.mark_uploaded(
                identifier=identifier,
                files=expected_keys,
                metadata=metadata,
                upc=rel.upc,
                qobuz_id=qobuz_id,
            )
        if delete_after:
            delete_local_release(rel)

    except Exception as e:
        err_msg = str(e).lower()
        if any(fatal in err_msg for fatal in FATAL_UPLOAD_ERRORS):
            print(f"  ⛔ [FATAL ERROR] Item locked or taken offline by IA: {e}")
            if store and hasattr(store, "record_upload_state"):
                store.record_upload_state(identifier, rel.upc, qobuz_id, rel.artist, rel.title, "taken_offline")
        else:
            print(f"  ! Upload failed: {e}")
            if store and hasattr(store, "record_upload_state"):
                store.record_upload_state(identifier, rel.upc, qobuz_id, rel.artist, rel.title, "failed")
    finally:
        for tmp in temp_cleanup_files:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
