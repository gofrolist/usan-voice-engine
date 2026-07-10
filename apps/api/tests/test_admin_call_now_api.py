import asyncio
import uuid

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.services import outbound_calls
from usan_api.settings import get_settings

_TZ = "America/Chicago"


def _contact(client, phone):
    return client.post(
        "/v1/admin/contacts",
        json={"name": "Call Target", "phone_e164": phone, "timezone": _TZ},
    ).json()["id"]


def test_call_now_dnc_blocked_returns_blocked(client, admin_session):
    phone = "+15551239001"
    cid = _contact(client, phone)
    # Put the number on the org's DNC list via the operator endpoint (same seeded org).
    assert (
        client.post(
            "/v1/dnc",
            json={"phone_e164": phone, "reason": "test"},
            headers={"Authorization": "Bearer " + "o" * 32},
        ).status_code
        == 201
    )

    r = client.post("/v1/admin/calls", json={"contact_id": cid})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "dnc_blocked"


def test_call_now_unknown_contact_404(client, admin_session):
    assert client.post("/v1/admin/calls", json={"contact_id": str(uuid.uuid4())}).status_code == 404


def test_call_now_blocked_when_dialing_paused(client, admin_session, monkeypatch):
    """Emergency stop applies to admin call-now, and rejects BEFORE the DNC advisory lock
    and audit write — so a blocked attempt leaves no rolled-back partial audit row
    (security review follow-up to the dialing-gate fix)."""
    cid = _contact(client, "+15551239009")
    monkeypatch.setenv("AUTONOMOUS_DIALING_PAUSED", "true")
    get_settings.cache_clear()
    r = client.post("/v1/admin/calls", json={"contact_id": cid})
    assert r.status_code == 503
    assert "paused" in r.json()["detail"]


def test_call_now_viewer_403(client, admin_session, async_database_url):
    cid = _contact(client, "+15551239002")
    from tests.conftest import _seed_admin_user_async

    asyncio.run(_seed_admin_user_async(async_database_url, "viewer-call@example.com", "viewer"))
    token = issue_session(
        "viewer-call@example.com",
        active_org_id=None,
        role=AdminRole.VIEWER,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.post("/v1/admin/calls", json={"contact_id": cid}).status_code == 403


def test_call_now_requires_session(bare_client):
    assert (
        bare_client.post("/v1/admin/calls", json={"contact_id": str(uuid.uuid4())}).status_code
        == 401
    )


def test_call_now_advisory_lock_held_through_dispatch(
    client, admin_session, async_database_url: str
):
    """Regression: the handler must NOT commit before calling create_and_dispatch.

    If an early commit were present, the transaction-scoped advisory lock acquired by
    lock_phone would release before the call is created, reopening the DNC TOCTOU window.
    We prove the lock is still held at dispatch entry by checking—from a separate
    superuser connection—that the call.enqueue audit row written before the dispatch call
    is NOT yet visible (uncommitted).  If a future dev re-adds the early commit, the row
    becomes visible and this assertion fails.
    """
    from starlette.exceptions import HTTPException as StarletteHTTPException

    audit_row_count_at_dispatch: list[int] = []

    async def _spy_dispatch(db, *, body, contact, settings):  # type: ignore[no-untyped-def]
        # Open a separate superuser connection (NullPool = fresh connection each time).
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                from sqlalchemy import text

                result = await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM admin_audit_log "
                        "WHERE action = 'call.enqueue' AND entity_id = :eid"
                    ),
                    {"eid": str(contact.id)},
                )
                audit_row_count_at_dispatch.append(int(result.scalar_one()))
        finally:
            await engine.dispose()

        # Raise so no real telephony is attempted and the test is deterministic.
        raise StarletteHTTPException(status_code=503, detail="stub")

    phone = "+15551239010"
    cid = _contact(client, phone)

    # Patch the module attribute the handler calls.
    original = outbound_calls.create_and_dispatch
    outbound_calls.create_and_dispatch = _spy_dispatch  # type: ignore[assignment]
    try:
        r = client.post("/v1/admin/calls", json={"contact_id": cid})
    finally:
        outbound_calls.create_and_dispatch = original

    assert r.status_code == 503, r.text
    assert len(audit_row_count_at_dispatch) == 1, "spy was not called"
    assert audit_row_count_at_dispatch[0] == 0, (
        "audit row was visible from a separate connection at dispatch entry — "
        "an early db.commit() must have been re-introduced, releasing the advisory lock"
    )
