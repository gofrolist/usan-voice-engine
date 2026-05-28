import pytest
from fastapi.testclient import TestClient

from usan_api.main import create_app
from usan_api.settings import get_settings


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    get_settings.cache_clear()
    app = create_app()
    return TestClient(app)
