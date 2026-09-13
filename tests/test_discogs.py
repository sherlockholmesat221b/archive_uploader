from archive_uploader.enrichment.discogs import DiscogsProvider
from archive_uploader.models import Release, TrackFile


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_discogs_fetch_maps_release_and_track_metadata(monkeypatch):
    monkeypatch.setenv("DISCOGS_TOKEN", "test-token")
    provider = DiscogsProvider()

    release_data = {
        "id": 13278659,
        "title": "Antologia I (1973 - 1978)",
        "artists": [{"name": "Inti-Illimani"}],
        "year": 2000,
        "released": "2000-05-15",
        "country": "Chile",
        "genres": ["Latin"],
        "styles": ["Folk", "Nueva Cancion"],
        "labels": [{"name": "Warner Music Chile", "catno": "MID 9715"}],
        "formats": [{"name": "CD", "qty": "1", "descriptions": ["Album"]}],
        "status": "Accepted",
        "tracklist": [
            {
                "position": "01",
                "title": "Alturas",
                "artists": [{"name": "Inti-Illimani"}],
                "extraartists": [{"name": "Horacio Salinas", "role": "Written-By"}],
            }
        ],
    }

    def fake_get(path, **params):
        if path == "/database/search":
            assert params["barcode"] == "685738354122"
            return {"results": [{"id": 13278659, "barcode": ["685738354122"]}]}
        assert path == "/releases/13278659"
        return release_data

    monkeypatch.setattr(provider, "_get", fake_get)

    rel = Release(
        kind="album",
        dir_or_file=__import__("pathlib").Path("."),
        title="Antologia I (1973 - 1978)",
        artist="Inti-Illimani",
        upc="685738354122",
        tracks=[TrackFile(path=__import__("pathlib").Path("01.flac"), title="Alturas")],
    )
    result = provider.fetch(rel)

    assert result["id"] == "13278659"
    assert result["country"] == "Chile"
    assert result["catalog_number"] == "MID 9715"
    assert result["styles"] == "Folk; Nueva Cancion"
    assert result["tracks"][0]["composer"] == "Horacio Salinas"
