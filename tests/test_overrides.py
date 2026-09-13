from archive_uploader.enrichment.overrides import (
    apply_overrides,
    load_overrides,
    override_path,
    write_starter_override,
)
from archive_uploader.models import Release


def test_override_path_for_album_is_inside_the_folder(tmp_path):
    album_dir = tmp_path / "Some Album"
    album_dir.mkdir()
    rel = Release(kind="album", dir_or_file=album_dir)

    assert override_path(rel) == album_dir / ".archive_meta.json"


def test_override_path_for_single_is_a_sibling_file(tmp_path):
    flac_file = tmp_path / "Track.flac"
    rel = Release(kind="single", dir_or_file=flac_file)

    assert override_path(rel) == tmp_path / "Track.archive_meta.json"


def test_overrides_win_over_previously_fetched_values(tmp_path):
    album_dir = tmp_path / "Some Album"
    album_dir.mkdir()
    rel = Release(kind="album", dir_or_file=album_dir, genre="Wrong Genre (from provider)")
    (album_dir / ".archive_meta.json").write_text('{"genre": "Correct Genre"}')

    apply_overrides(rel, load_overrides(rel))

    assert rel.genre == "Correct Genre"


def test_blank_override_fields_do_not_clobber_existing_values(tmp_path):
    # A starter template has every field blank; that must not wipe out
    # what enrichment already found just because the file exists.
    album_dir = tmp_path / "Some Album"
    album_dir.mkdir()
    rel = Release(kind="album", dir_or_file=album_dir, genre="From Provider")
    write_starter_override(rel)

    apply_overrides(rel, load_overrides(rel))

    assert rel.genre == "From Provider"


def test_external_links_and_provider_ids_are_merged_in(tmp_path):
    album_dir = tmp_path / "Some Album"
    album_dir.mkdir()
    rel = Release(kind="album", dir_or_file=album_dir)
    (album_dir / ".archive_meta.json").write_text(
        '{"external_links": [{"service": "Discogs", "url": "https://discogs.com/x"}], '
        '"provider_ids": {"discogs": "12345"}}'
    )

    apply_overrides(rel, load_overrides(rel))

    assert rel.external_links[0].service == "Discogs"
    assert rel.provider_ids["discogs"] == "12345"


def test_write_starter_override_does_not_overwrite_existing_file(tmp_path):
    album_dir = tmp_path / "Some Album"
    album_dir.mkdir()
    rel = Release(kind="album", dir_or_file=album_dir)
    path = override_path(rel)
    path.write_text('{"genre": "Hand Written"}')

    write_starter_override(rel)

    assert load_overrides(rel)["genre"] == "Hand Written"


def test_identifier_override_is_applied(tmp_path):
    album_dir = tmp_path / "Some Album"
    album_dir.mkdir()
    rel = Release(kind="album", dir_or_file=album_dir, identifier="")

    (album_dir / ".archive_meta.json").write_text(
        '{"identifier": "flac-my-custom-identifier"}'
    )
    apply_overrides(rel, load_overrides(rel))

    assert rel.identifier == "flac-my-custom-identifier"


def test_review_serialization_round_trips_identifier_and_tracks(tmp_path):
    from archive_uploader.enrichment.overrides import release_to_override, save_review_override
    from archive_uploader.models import TrackFile

    album_dir = tmp_path / "Some Album"
    album_dir.mkdir()
    track = TrackFile(
        path=album_dir / "01.flac",
        title="Reviewed Track",
        artist="Reviewed Artist",
        tracknumber="1",
    )
    rel = Release(
        kind="album",
        dir_or_file=album_dir,
        identifier="flac-reviewed-id",
        title="Reviewed Album",
        tracks=[track],
    )

    assert release_to_override(rel)["identifier"] == "flac-reviewed-id"
    path = save_review_override(rel)
    data = load_overrides(rel)
    assert path.exists()
    assert data["identifier"] == "flac-reviewed-id"
    assert data["tracks"]["01.flac"]["title"] == "Reviewed Track"
