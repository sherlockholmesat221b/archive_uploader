from archive_uploader.ia.payload import _build_subjects, build_ia_payload
from archive_uploader.models import Release, TrackFile


def test_payload_contains_all_track_isrcs_and_rich_metadata(tmp_path, monkeypatch):
    album = tmp_path / "Album"
    album.mkdir()
    tracks = [
        TrackFile(
            path=album / "01.flac",
            title="One",
            artist="Artist",
            tracknumber="01",
            isrc="AA0000000001",
            composer="Composer One",
        ),
        TrackFile(
            path=album / "02.flac",
            title="Two",
            artist="Artist",
            tracknumber="02",
            isrc="AA0000000002",
            composer="Composer Two",
        ),
    ]
    rel = Release(
        kind="album",
        dir_or_file=album,
        title="Album",
        artist="Artist",
        country="Chile",
        genre="Folclore",
        styles="Nueva canción; Folk",
        label="Warner Music Chile",
        catalog_number="MID 9715",
        format="CD (Album)",
        release_type="release",
        release_status="Accepted",
        upc="685738354122",
        tracks=tracks,
        provider_ids={
            "MusicBrainz": "mb-id",
            "Discogs": "13278659",
        },
    )

    # Avoid packaging/encoding in this metadata-only test.
    monkeypatch.setattr("archive_uploader.ia.payload.compute_script_hash", lambda: "x" * 64)
    monkeypatch.setattr("archive_uploader.ia.payload.get_repo_ref", lambda: "test")
    monkeypatch.setattr("archive_uploader.ia.payload.derive_opus_file", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr("archive_uploader.ia.payload.create_clean_zip", lambda *a, **k: None)

    identifier, metadata, _, cleanup = build_ia_payload(
        rel, "opensource_audio", "audio", identifier="custom-id"
    )

    assert identifier == "custom-id"
    assert metadata["upc"] == "685738354122"
    assert metadata["barcode"] == "685738354122"
    assert metadata["musicbrainz_id"] == "mb-id"
    assert metadata["discogs_id"] == "13278659"
    assert metadata["country"] == "Chile"
    assert metadata["style"] == ["Nueva canción", "Folk"]
    assert metadata["catalog_number"] == "MID 9715"
    assert metadata["isrc"] == ["AA0000000001", "AA0000000002"]
    assert metadata["track_isrc"] == ["01: AA0000000001", "02: AA0000000002"]
    assert "Composer One" in metadata["composer"]
    assert "Chile" in metadata["subject"]
    assert "Nueva canción" in metadata["subject"]


def test_subjects_are_metadata_derived_not_keyword_stuffed(tmp_path):
    rel = Release(
        kind="album",
        dir_or_file=tmp_path,
        title="Album",
        artist="Artist",
        genre="Folk",
        styles="Nueva canción",
        label="Example Label",
        country="Chile",
    )
    subjects = _build_subjects(rel)
    assert "Artist" in subjects
    assert "Folk" in subjects
    assert "Nueva canción" in subjects
    assert "Chile" in subjects
    assert "Country: Chile" in subjects
    assert "random SEO keyword" not in subjects
