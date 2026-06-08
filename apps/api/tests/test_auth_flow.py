import asyncio
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import oauth
from usan_api.admin_session import SESSION_COOKIE_NAME, TX_COOKIE_NAME, issue_tx
from usan_api.settings import get_settings


async def _seed(async_database_url: str, email: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, role, added_by) "
                    "VALUES (:e, CAST('admin' AS admin_role), 'test') "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email.lower()},
            )
    finally:
        await engine.dispose()


def test_login_redirects_to_google_and_sets_tx_cookie(sso_client):
    r = sso_client.get("/v1/auth/login")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert urlsplit(loc).netloc == "accounts.google.com"
    q = parse_qs(urlsplit(loc).query)
    assert q["code_challenge_method"] == ["S256"]
    assert TX_COOKIE_NAME in r.cookies


def test_login_503_when_sso_disabled(client):
    r = client.get("/v1/auth/login")
    assert r.status_code == 503


def test_callback_success_sets_session_and_redirects(sso_client, async_database_url, monkeypatch):
    asyncio.run(_seed(async_database_url, "alice@example.com"))

    async def _fake_exchange(settings, *, code, code_verifier):
        return "fake-id-token"

    def _fake_verify(settings, raw_token):
        return {"email": "alice@example.com", "email_verified": True}

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange)
    monkeypatch.setattr(oauth, "verify_id_token", _fake_verify)

    tx = issue_tx("STATE123", "verifier", get_settings())
    sso_client.cookies.set(TX_COOKIE_NAME, tx)
    r = sso_client.get("/v1/auth/callback", params={"code": "abc", "state": "STATE123"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert SESSION_COOKIE_NAME in r.cookies


def test_callback_denied_for_non_allowlisted(sso_client, monkeypatch):
    async def _fake_exchange(settings, *, code, code_verifier):
        return "fake-id-token"

    def _fake_verify(settings, raw_token):
        return {"email": "stranger@example.com", "email_verified": True}

    monkeypatch.setattr(oauth, "exchange_code", _fake_exchange)
    monkeypatch.setattr(oauth, "verify_id_token", _fake_verify)

    tx = issue_tx("S", "v", get_settings())
    sso_client.cookies.set(TX_COOKIE_NAME, tx)
    r = sso_client.get("/v1/auth/callback", params={"code": "abc", "state": "S"})
    assert r.status_code == 403


def test_callback_state_mismatch_is_400(sso_client):
    tx = issue_tx("EXPECTED", "v", get_settings())
    sso_client.cookies.set(TX_COOKIE_NAME, tx)
    r = sso_client.get("/v1/auth/callback", params={"code": "abc", "state": "WRONG"})
    assert r.status_code == 400


def test_me_requires_session_then_returns_identity(client, admin_session):
    r = client.get("/v1/auth/me")
    assert r.status_code == 200
    assert r.json() == {"email": "admin@example.com", "role": "admin"}


def test_logout_clears_cookie(client, admin_session):
    r = client.post("/v1/auth/logout")
    assert r.status_code == 204
    assert SESSION_COOKIE_NAME in r.headers.get("set-cookie", "")
