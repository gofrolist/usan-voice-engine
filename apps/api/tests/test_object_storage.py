import datetime
from types import SimpleNamespace

import pytest

from usan_api import object_storage


def test_parse_gs_uri_ok():
    assert object_storage._parse_gs_uri("gs://b/recordings/2026-06-02/x.ogg") == (
        "b",
        "recordings/2026-06-02/x.ogg",
    )


@pytest.mark.parametrize("bad", ["http://b/x", "gs://b", "gs://b/", "gs:///x", "gs://"])
def test_parse_gs_uri_rejects_malformed(bad):
    with pytest.raises(ValueError, match="gs://"):
        object_storage._parse_gs_uri(bad)


def test_generate_signed_url_keyless(monkeypatch):
    captured: dict = {}
    creds = SimpleNamespace(
        service_account_email="sa@example.iam.gserviceaccount.com",
        token="ya29.token",
        refresh=lambda request: None,
    )
    monkeypatch.setattr(object_storage.google.auth, "default", lambda scopes=None: (creds, "proj"))

    class _Blob:
        def generate_signed_url(self, **kwargs):
            captured.update(kwargs)
            return "https://signed.example/url"

    class _Bucket:
        def blob(self, name):
            captured["blob_name"] = name
            return _Blob()

    class _Client:
        def __init__(self, credentials=None):
            captured["credentials"] = credentials

        def bucket(self, name):
            captured["bucket"] = name
            return _Bucket()

    monkeypatch.setattr(object_storage.storage, "Client", _Client)

    url = object_storage.generate_signed_url("gs://b/recordings/2026-06-02/x.ogg", 3600)

    assert url == "https://signed.example/url"
    assert captured["bucket"] == "b"
    assert captured["blob_name"] == "recordings/2026-06-02/x.ogg"
    assert captured["version"] == "v4"
    assert captured["method"] == "GET"
    assert captured["expiration"] == datetime.timedelta(seconds=3600)
    assert captured["service_account_email"] == "sa@example.iam.gserviceaccount.com"
    assert captured["access_token"] == "ya29.token"
