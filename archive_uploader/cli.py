from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .enrichment import enrich
from .enrichment.overrides import apply_overrides, load_overrides, write_starter_override
from .ia import upload_release
from .mega_backup import mega_backup_release
from .scanning import scan_directory
from .state.store import SQLiteStateStore

def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable FLAC to Internet Archive automated uploader.")
    parser.add_argument("--root", default=".", help="Root directory containing FLAC folders/singles")
    parser.add_argument("--collection", default="opensource_audio", help="Internet Archive collection")
    parser.add_argument("--mediatype", default="audio", help="Internet Archive mediatype")
    parser.add_argument("--dry-run", action="store_true", help="Scan and resolve without uploading")
    parser.add_argument(
        "--delete-after-upload", action="store_true", help="Delete local files/folders after verified upload"
    )
    parser.add_argument(
        "--skip-enrichment", action="store_true",
        help="Skip provider metadata lookups entirely; use only local tags and any .archive_meta.json "
             "override (for releases nothing online will ever match)",
    )
    parser.add_argument(
        "--manual-metadata", action="store_true",
        help="Write (or reuse) each release's .archive_meta.json template and stop before uploading it, "
             "so every field -- release and per-track -- can be hand-filled first. Re-run without this "
             "flag once you're done editing.",
    )
    parser.add_argument(
        "--mega-backup", action="store_true",
        help="Also back up each release's original local files to mega.nz via megatools, before uploading",
    )

    args = parser.parse_args()
    root_path = Path(args.root).resolve()

    # Force exclusive SQLite persistence engine
    state = SQLiteStateStore()

    print(f"Scanning target path: {root_path}")
    releases = scan_directory(root_path)

    if not releases:
        print("No FLAC releases or folders found.")
        return

    print(f"Found {len(releases)} item(s) to process.")

    try:
        for rel in releases:
            if args.skip_enrichment:
                apply_overrides(rel, load_overrides(rel))
            else:
                enrich(rel)

            if args.manual_metadata:
                path = write_starter_override(rel)
                print(f"  \u270e Metadata template ready at: {path}")
                print("     Fill in whatever fields matter, then re-run without --manual-metadata to upload.")
                continue

            if args.mega_backup and not args.dry_run:
                mega_backup_release(rel)

            upload_release(rel, args.collection, args.mediatype, args.dry_run, args.delete_after_upload, state)
    except KeyboardInterrupt:
        print("\nProcess canceled by user. Local files preserved. Exiting...")
        sys.exit(0)

    print("\nBatch processing completed.")

if __name__ == "__main__":
    main()
