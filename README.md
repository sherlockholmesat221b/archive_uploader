# archive_uploader

Resumable FLAC/album uploader for the Internet Archive, with online
metadata enrichment (Qobuz, MusicBrainz, Wikipedia), manual overrides,
multi-device-safe state, and free versioning of both the state and the
code.

## Layout

```
archive_uploader/
  cli.py              CLI entry point — orchestration only
  config.py           paths, device identity, shared constants
  models.py           Release / TrackFile / ExternalLink dataclasses
  scanning.py         local FLAC tag reading + cover detection (no network)
  packaging.py        opus derivation, zip packaging, file-key flattening
  textutils.py        slugify()
  state/
    store.py            StateStore interface
    log_store.py         append-only per-device event log (the real implementation)
    backup.py            numbered snapshots of the state logs
  enrichment/
    base.py              Provider interface
    qobuz.py, musicbrainz.py, wikipedia.py
    overrides.py          manual metadata override system
    pipeline.py           merges tags -> providers -> overrides
  ia/
    identifiers.py        slug + legacy-id resolution
    payload.py             description/metadata/file-manifest building
    uploader.py             upload execution, resume/repair logic
tools/
  snap.sh              zero-effort auto-incrementing git snapshot of the code
tests/                 pytest, covering the load-bearing "looks removable" logic
DECISIONS.md           why the non-obvious things exist — read before editing
```

## Setup

```bash
pip install -e ".[dev]"
```

`kabooz` (Qobuz) isn't a hard dependency — `QobuzProvider` just returns
no match if it's missing or unconfigured, so the rest of the pipeline
still works without it.

## Running

```bash
python -m archive_uploader --root /path/to/music --dry-run
python -m archive_uploader --root /path/to/music --delete-after-upload
python -m archive_uploader --root /path/to/music --review
```

`--review` (also `--interactive`) opens a terminal UI after metadata
enrichment. Use the arrow keys to select a release field or track, `Enter`
to edit it, `S` to save the reviewed metadata without uploading, `A` to
approve it and continue to the upload, and `Q` to skip the release. The
Internet Archive identifier is editable in the same screen; a non-empty
identifier overrides automatic identifier resolution.

## Adding metadata by hand (rare items, or correcting a fetched value)

Drop a `.archive_meta.json` next to a release (inside the folder for
an album, as `<name>.archive_meta.json` next to a single file). Any
field you set there wins over both local tags and whatever the online
providers found — see `enrichment/overrides.py` for the exact schema.
Re-running the uploader never overwrites it.

## Multi-device state

Each device writes only to its own `~/.config/archive_uploader/state/logs/<device_id>.ndjson`.
Sync that folder however you like (Syncthing, etc.) — since no two
devices ever write the same file, there's no corruption risk regardless
of timing. The *actual* duplicate-upload guard is the live check
against Internet Archive in `ia/uploader.py`; the local log is only a
speed optimization on top of that.

## Backups and versioning

- **State**: `state.backup.snapshot_logs()` (called automatically at
  the end of every CLI run) copies each device's log to a numbered file
  in `state/backups/`, e.g. `laptop-a1b2c3d4_00042.ndjson`. Since the
  log is append-only, an old numbered copy *is* an old version — no
  separate versioning system needed.
- **Code**: `tools/snap.sh` auto-commits everything to git under a
  zero-effort incrementing tag (`snap-00001`, `snap-00002`, ...) — no
  commit message ever required. Run it before/after letting anything
  (including an AI) edit the code, so there's always a clean diff/revert
  point:
  ```bash
  ./tools/snap.sh
  git diff snap-00041 snap-00042        # see exactly what changed
  git checkout snap-00041 -- archive_uploader/some_file.py   # revert one file
  ```

## Adding a new metadata provider

Write a new `enrichment/<service>.py` implementing `Provider.fetch()`,
returning at least `id`/`url` if it matches. Register it in
`enrichment/pipeline.py`'s `DEFAULT_PROVIDERS`. Nothing in
`ia/payload.py` needs to change — badge rendering and the
`external-identifier`/`<name>_id` metadata fields are generic over
whatever providers matched.

## Tests

```bash
pytest
```

Focused on the logic that looks like dead weight to a skimming editor
but isn't: legacy identifier fallback, multi-disc file-key flattening,
the state log's per-device write isolation, and override precedence.
See `DECISIONS.md` for the reasoning behind each.
# test change
# change
# change
