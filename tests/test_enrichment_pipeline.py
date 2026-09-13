from typing import Optional

from archive_uploader.enrichment.base import Provider
from archive_uploader.enrichment.pipeline import enrich
from archive_uploader.models import Release


class FakeProvider(Provider):
    name = "FakeService"
    logo_url = "https://example.com/logo.png"

    def __init__(self, result: Optional[dict]):
        self._result = result

    def fetch(self, rel: Release) -> Optional[dict]:
        return self._result


def test_provider_result_is_merged_into_blank_fields(tmp_path):
    rel = Release(kind="album", dir_or_file=tmp_path, artist="Artist", title="Title")
    provider = FakeProvider({"id": "abc123", "url": "https://example.com/abc123", "genre": "Jazz"})

    enrich(rel, providers=[provider])

    assert rel.genre == "Jazz"
    assert rel.provider_ids["FakeService"] == "abc123"
    assert rel.external_links[0].url == "https://example.com/abc123"
    assert rel.external_links[0].logo_url == "https://example.com/logo.png"


def test_provider_never_clobbers_a_field_already_set_locally(tmp_path):
    rel = Release(kind="album", dir_or_file=tmp_path, artist="Artist", title="Title", genre="From Tags")
    provider = FakeProvider({"genre": "From Provider"})

    enrich(rel, providers=[provider])

    assert rel.genre == "From Tags"


def test_override_file_wins_over_a_provider_result(tmp_path):
    (tmp_path / ".archive_meta.json").write_text('{"genre": "From Override File"}')
    rel = Release(kind="album", dir_or_file=tmp_path, artist="Artist", title="Title")
    provider = FakeProvider({"genre": "From Provider"})

    enrich(rel, providers=[provider])

    assert rel.genre == "From Override File"


def test_a_failing_provider_does_not_stop_the_others(tmp_path):
    class BrokenProvider(Provider):
        name = "Broken"

        def fetch(self, rel: Release) -> Optional[dict]:
            raise RuntimeError("simulated failure")

    rel = Release(kind="album", dir_or_file=tmp_path, artist="Artist", title="Title")
    good_provider = FakeProvider({"genre": "Still Works"})

    enrich(rel, providers=[BrokenProvider(), good_provider])

    assert rel.genre == "Still Works"


def test_no_match_provider_returning_none_is_a_no_op(tmp_path):
    rel = Release(kind="album", dir_or_file=tmp_path, artist="Artist", title="Title")
    enrich(rel, providers=[FakeProvider(None)])

    assert rel.provider_ids == {}
    assert rel.external_links == []


def test_provider_track_credits_are_merged(tmp_path):
    from archive_uploader.models import TrackFile

    track = TrackFile(path=tmp_path / "01.flac", title="Alturas")
    rel = Release(kind="album", dir_or_file=tmp_path, artist="Artist", title="Title", tracks=[track])
    provider = FakeProvider({
        "tracks": [
            {"position": "1", "title": "Alturas", "artist": "Guest Artist", "composer": "Composer"}
        ]
    })

    enrich(rel, providers=[provider])

    assert rel.tracks[0].artist == "Guest Artist"
    assert rel.tracks[0].composer == "Composer"
