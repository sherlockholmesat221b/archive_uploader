from __future__ import annotations

import argparse
import sys
import shutil
from pathlib import Path

from .enrichment import enrich
from .enrichment.overrides import (
    apply_overrides,
    load_overrides,
    write_starter_override,
)
from .review import review_release
from .ia import upload_release
from .mega_backup import mega_backup_release
from .scanning import scan_directory
from .state.store import SQLiteStateStore
from .sync import serve, sync
from .ui import stage


def run_sync(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="archive-uploader sync",
        description="Synchronize archive_uploader state over the network.",
    )

    parser.add_argument(
        "peer",
        nargs="?",
        help="IP address or hostname of the peer device.",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=47321,
        help="Sync TCP port (default: 47321).",
    )

    parser.add_argument(
        "--listen",
        action="store_true",
        help="Listen for another archive-uploader device.",
    )

    args = parser.parse_args(argv)

    if args.listen:
        if args.peer is not None:
            parser.error(
                "--listen cannot be combined with a peer address"
            )

        print(
            f"archive-uploader sync: listening on "
            f"0.0.0.0:{args.port}"
        )

        serve(
            host="0.0.0.0",
            port=args.port,
        )
        return

    if args.peer is None:
        parser.error(
            "provide a peer address or use --listen"
        )

    print(
        f"archive-uploader sync: connecting to "
        f"{args.peer}:{args.port}"
    )

    sync(
        args.peer,
        port=args.port,
    )


def main() -> None:

    # ---------------------------------------------------------------
    # Network state synchronization
    #
    #     archive-uploader sync 192.168.1.20
    #
    # or:
    #
    #     archive-uploader sync --listen
    #
    # Everything else continues through the original uploader CLI.
    # ---------------------------------------------------------------

    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        run_sync(sys.argv[2:])
        return

    # ---------------------------------------------------------------
    # Original archive-uploader CLI
    # ---------------------------------------------------------------

    parser = argparse.ArgumentParser(
        description=(
            "Resumable FLAC to Internet Archive "
            "automated uploader."
        )
    )

    parser.add_argument(
        "--root",
        default=".",
        help="Root directory containing FLAC folders/singles",
    )

    parser.add_argument(
        "--collection",
        default="opensource_audio",
        help="Internet Archive collection",
    )

    parser.add_argument(
        "--mediatype",
        default="audio",
        help="Internet Archive mediatype",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and resolve without uploading",
    )

    parser.add_argument(
        "--delete-after-upload",
        action="store_true",
        help=(
            "Delete local files/folders after "
            "verified upload"
        ),
    )

    parser.add_argument(
        "--skip-enrichment",
        action="store_true",
        help=(
            "Skip provider metadata lookups entirely; "
            "use only local tags and any .archive_meta.json "
            "override (for releases nothing online will ever match)"
        ),
    )

    parser.add_argument(
        "--review",
        "--interactive",
        dest="review",
        action="store_true",
        help=(
            "Open an interactive TUI after enrichment; "
            "edit metadata/IA identifier, "
            "S saves and continues, "
            "A approves and uploads, "
            "Q skips the release"
        ),
    )

    parser.add_argument(
        "--manual-metadata",
        action="store_true",
        help=(
            "Write (or reuse) each release's "
            ".archive_meta.json template and stop before uploading it, "
            "so every field -- release and per-track -- "
            "can be hand-filled first. "
            "Re-run without this flag once you're done editing."
        ),
    )

    parser.add_argument(
        "--mega-backup",
        action="store_true",
        help=(
            "Also back up each release's original local files "
            "to mega.nz via megatools, before uploading"
        ),
    )

    parser.add_argument("--qobuz", nargs="+", metavar="ID_OR_URL",
                        help="Download these Qobuz albums via kabooz, then upload them")
    parser.add_argument("--quality", default=None,
                        help="kabooz quality for --qobuz (default: kabooz config)")

    args = parser.parse_args()

    root_path = Path(args.root).resolve()

    # Force exclusive SQLite persistence engine
    state = SQLiteStateStore()

    staging: list[Path] = []

    if args.qobuz:
        from .sources.kabooz import fetch_album

        stage(1, 5, "Qobuz download", f"{len(args.qobuz)} album(s) via kabooz")
        releases = []
        for ref in args.qobuz:
            try:
                got = fetch_album(ref, state, args.quality)
            except Exception as e:
                print(f"  ! {ref}: {e}")
                continue
            if got:
                releases.append(got[0])
                staging.append(got[1])
    else:
        stage(1, 5, "Scanning", str(root_path))
        print("  → Discovering FLAC releases and reading local tags")
        releases = scan_directory(root_path)

    if not releases:
        print("No FLAC releases or folders found.")
        return

    print(
        f"Found {len(releases)} item(s) to process."
    )

    try:
        for index, rel in enumerate(
            releases,
            start=1,
        ):
            print("\n" + "═" * 72)
            print(
                f"Release {index}/{len(releases)}: "
                f"{rel.artist} — {rel.title}"
            )
            print("═" * 72)

            stage(
                2,
                5,
                "Metadata enrichment",
                f"{rel.artist} — {rel.title}",
            )

            if args.skip_enrichment:
                print(
                    "  → Online providers disabled; "
                    "applying local overrides only"
                )

                apply_overrides(
                    rel,
                    load_overrides(rel),
                )

            else:
                enrich(rel)

            if args.review:
                stage(
                    3,
                    5,
                    "Interactive review",
                    "edit, compare, save, or approve",
                )

                result = review_release(rel)

                if result == "cancel":
                    print(
                        "  -> Review canceled; "
                        "release skipped."
                    )
                    continue

                if result == "save":
                    print(
                        "  -> Reviewed metadata saved; "
                        "release skipped. Re-run --review "
                        "to approve/upload."
                    )
                    continue

                print("  -> Metadata approved.")

            if args.manual_metadata:
                stage(
                    3,
                    5,
                    "Manual metadata",
                    "writing review template",
                )

                path = write_starter_override(rel)

                print(
                    f"  ✎ Metadata template ready at: {path}"
                )

                print(
                    "     Fill in whatever fields matter, "
                    "then re-run without "
                    "--manual-metadata to upload."
                )

                continue

            if args.mega_backup and not args.dry_run:
                stage(
                    4,
                    5,
                    "Mega backup",
                    "preserving original local files",
                )

                mega_backup_release(rel)

            elif not args.mega_backup:
                print(
                    "\n[4/5] Mega backup — disabled"
                )

            stage(
                5,
                5,
                "Internet Archive",
                "derivation, packaging, "
                "manifest checks, upload",
            )

            upload_release(
                rel,
                args.collection,
                args.mediatype,
                args.dry_run,
                args.delete_after_upload or bool(args.qobuz),
                state,
            )

    except KeyboardInterrupt:
        print(
            "\nProcess canceled by user. "
            "Local files preserved. Exiting..."
        )
        sys.exit(0)

    if not args.dry_run:
        for d in staging:
            if not any(d.rglob("*.flac")):
                shutil.rmtree(d, ignore_errors=True)

    print("\nBatch processing completed.")


if __name__ == "__main__":
    main()
