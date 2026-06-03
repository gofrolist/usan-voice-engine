import datetime
from types import SimpleNamespace

import pytest

from usan_api import object_storage


@pytest.fixture(autouse=True)
def _reset_signing_cache():
    # The module caches ADC credentials; reset around each test for isolation.
    object_storage._signing_credentials = None
    yield
    object_storage._signing_credentials = None


def _stub_storage(monkeypatch, captured):
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


def test_parse_gs_uri_ok():
    assert object_storage._parse_gs_uri("gs://b/recordings/2026-06-02/x.ogg") == (
        "b",
        "recordings/2026-06-02/x.ogg",
    )


@pytest.mark.parametrize("bad", ["http://b/x", "gs://b", "gs://b/", "gs:///x", "gs://"])
def test_parse_gs_uri_rejects_malformed(bad):
    with pytest.raises(ValueError, match="gs://"):
        object_storage._parse_gs_uri(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "gs://b/../etc/passwd",
        "gs://b/recordings/../../secret",
        "gs://b//leading-slash",
    ],
)
def test_parse_gs_uri_rejects_path_traversal(bad):
    with pytest.raises(ValueError, match="unsafe path"):
        object_storage._parse_gs_uri(bad)


def test_generate_signed_url_keyless(monkeypatch):
    captured: dict = {}
    creds = SimpleNamespace(
        service_account_email="sa@example.iam.gserviceaccount.com",
        token="ya29.token",
        valid=True,
        refresh=lambda request: None,
    )
    monkeypatch.setattr(object_storage.google.auth, "default", lambda scopes=None: (creds, "proj"))
    _stub_storage(monkeypatch, captured)

    url = object_storage.generate_signed_url("gs://b/recordings/2026-06-02/x.ogg", 3600)

    assert url == "https://signed.example/url"
    assert captured["bucket"] == "b"
    assert captured["blob_name"] == "recordings/2026-06-02/x.ogg"
    assert captured["version"] == "v4"
    assert captured["method"] == "GET"
    assert captured["expiration"] == datetime.timedelta(seconds=3600)
    assert captured["service_account_email"] == "sa@example.iam.gserviceaccount.com"
    assert captured["access_token"] == "ya29.token"


def test_generate_signed_url_rejects_bucket_mismatch(monkeypatch):
    creds = SimpleNamespace(
        service_account_email="sa@example.iam.gserviceaccount.com",
        token="ya29.token",
        valid=True,
        refresh=lambda request: None,
    )
    monkeypatch.setattr(object_storage.google.auth, "default", lambda scopes=None: (creds, "proj"))
    _stub_storage(monkeypatch, {})
    with pytest.raises(ValueError, match="does not match expected"):
        object_storage.generate_signed_url(
            "gs://attacker-bucket/x.ogg", 3600, expected_bucket="usan-rec"
        )


def test_generate_signed_url_accepts_matching_bucket(monkeypatch):
    captured: dict = {}
    creds = SimpleNamespace(
        service_account_email="sa@example.iam.gserviceaccount.com",
        token="ya29.token",
        valid=True,
        refresh=lambda request: None,
    )
    monkeypatch.setattr(object_storage.google.auth, "default", lambda scopes=None: (creds, "proj"))
    _stub_storage(monkeypatch, captured)
    url = object_storage.generate_signed_url(
        "gs://usan-rec/x.ogg", 3600, expected_bucket="usan-rec"
    )
    assert url == "https://signed.example/url"
    assert captured["bucket"] == "usan-rec"


def test_signing_credentials_are_cached(monkeypatch):
    # google.auth.default() + refresh() are network round-trips; they must run once
    # and be reused across signings until the token expires.
    default_calls: list[int] = []
    refresh_calls: list[int] = []

    def _refresh(request):
        refresh_calls.append(1)
        creds.valid = True  # a fresh token makes the credentials valid

    creds = SimpleNamespace(
        service_account_email="sa@example.iam.gserviceaccount.com",
        token="ya29.token",
        valid=False,
        refresh=_refresh,
    )

    def _default(scopes=None):
        default_calls.append(1)
        return creds, "proj"

    monkeypatch.setattr(object_storage.google.auth, "default", _default)
    _stub_storage(monkeypatch, {})

    object_storage.generate_signed_url("gs://b/a.ogg", 3600)
    object_storage.generate_signed_url("gs://b/c.ogg", 3600)

    assert default_calls == [1]  # ADC fetched once, then cached
    assert refresh_calls == [1]  # refreshed once; the warm second call sees valid creds


def test_parse_gs_uri_rejects_dot_and_percent_segments():
    import pytest

    from usan_api.object_storage import _parse_gs_uri

    for bad in ("gs://b/a/./c", "gs://b/a/%2e%2e/c", "gs://b/a%2Fb", "gs://b//leading"):
        with pytest.raises(ValueError, match="unsafe path"):
            _parse_gs_uri(bad)
