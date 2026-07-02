# Knowledge Bases — admin UI + native API (text-only v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give org-admins a native, RLS-scoped admin UI to create knowledge bases and manage their **text** sources, reusing the existing KB tables and ingestion poller.

**Architecture:** A native `/v1/admin/knowledge-bases` FastAPI router (session-cookie auth, RLS-scoped, `require_admin_role`) reuses `repositories/knowledge_bases.py` and the running `kb_ingestion_poller`. A shared `add_text_sources` helper is extracted from the compat `kb_service` so the compat surface and the native router persist sources and (re)trigger ingestion identically. A React admin-ui feature (`features/knowledgeBases/`) provides list, create, and detail-with-sources views under the Config nav group, polling while a KB ingests.

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy async / Pydantic v2 / pytest (api); React 18 / Vite / TypeScript / Tailwind v4 / @tanstack/react-query v5 / vitest + Testing Library (admin-ui).

## Global Constraints

- **Text-only v1.** No file upload, no URL sources. The backend already rejects files/URLs with `422 "only text sources are supported"`; the native surface accepts only `{title, text}`.
- **No new migration.** Reuses migration 0047 tables (`knowledge_bases`, `knowledge_base_sources`, `knowledge_base_chunks`).
- **Raw UUIDs on the native plane** (not compat `kb_`/`source_`-encoded tokens).
- **RLS isolation:** every route uses `get_tenant_db`; a cross-org id resolves to `404` (never leak existence).
- **Role gating:** reads gate on the router-level `require_admin_session` (any VIEWER+ session); writes add `Depends(require_admin_role(AdminRole.ADMIN))`. Frontend hides write controls behind `useIsAdmin()`.
- **KB status model:** `in_progress` → `complete` / `error`. `create_kb` sets `in_progress`; the poller lease-claims `in_progress` KBs and flips them to `complete`/`error`. There is **no** "pending"/"idle" state.
- **Never echo `content`.** Source `content` (raw PHI-adjacent text) is never returned by any read endpoint — only `title`, derived status, and timestamps.
- **Python:** type hints required, line-length 100, `ruff check` + `ruff format` + `mypy` clean. Run `uv run pytest` from `apps/api`.
- **TypeScript:** no `any`, run `npm run typecheck` (tsc) before considering frontend done — CI runs it and local `npm run lint` does not.
- **Commit format:** `type(scope): description` — scope `api` for backend, `admin-ui` for frontend.

---

### Task 1: Shared `add_text_sources` helper + compat `kb_service` refactor

Extract the "persist text sources + reset the KB to `in_progress`" logic out of the compat service into a helper both surfaces call, so they cannot drift. Behavior of the compat surface must be unchanged (existing compat tests stay green).

**Files:**
- Create: `apps/api/src/usan_api/compat/kb_sources.py`
- Modify: `apps/api/src/usan_api/compat/services/kb_service.py`
- Test: `apps/api/tests/test_kb_sources_helper.py` (new) + existing compat KB tests must still pass

**Interfaces:**
- Produces:
  - `usan_api.compat.kb_sources.TextSource` — `@dataclass(frozen=True)` with `title: str`, `text: str`.
  - `async def add_text_sources(db: AsyncSession, kb_id: uuid.UUID, texts: list[TextSource]) -> None` — flush-only; persists each text source (setting `content_url` to the internal reference) and, if `texts` is non-empty, calls `repo.mark_in_progress(db, kb_id)`. Caller commits.
- Consumes (existing): `repositories.knowledge_bases` (`add_source`, `mark_in_progress`), `usan_api.compat.ids.encode_kb_source_id`.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_kb_sources_helper.py`:

```python
"""Unit test for the shared KB text-source helper (Task 1)."""

import uuid

import pytest

from usan_api.compat.kb_sources import TextSource, add_text_sources
from usan_api.repositories import knowledge_bases as repo


@pytest.mark.asyncio
async def test_add_text_sources_persists_and_marks_in_progress(app_session):
    kb = await repo.create_kb(
        app_session, name="KB", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    # Simulate ingestion having completed so we can prove the helper resets it.
    await repo.set_status(app_session, kb.id, "complete")

    await add_text_sources(
        app_session, kb.id, [TextSource(title="Doc A", text="hello world")]
    )

    sources = await repo.get_sources(app_session, kb.id)
    assert len(sources) == 1
    assert sources[0].title == "Doc A"
    assert sources[0].content == "hello world"
    assert sources[0].content_url  # non-empty internal reference set by the helper
    refreshed = await repo.get_kb(app_session, kb.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"  # reset for re-ingestion


@pytest.mark.asyncio
async def test_add_text_sources_empty_is_noop(app_session):
    kb = await repo.create_kb(
        app_session, name="KB2", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    await repo.set_status(app_session, kb.id, "complete")

    await add_text_sources(app_session, kb.id, [])

    assert not await repo.get_sources(app_session, kb.id)
    refreshed = await repo.get_kb(app_session, kb.id)
    assert refreshed is not None
    assert refreshed.status == "complete"  # untouched when nothing added
```

> Note: `app_session` is the RLS-scoped session fixture used across the KB repo tests (see `apps/api/tests/test_knowledge_bases_repo.py` for its usage — reuse it verbatim). If those tests decorate with `@pytest.mark.asyncio`, match that; if they use `anyio`, match that instead. Check one sibling test's decorator before running.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/test_kb_sources_helper.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'usan_api.compat.kb_sources'`.

- [ ] **Step 3: Create the shared helper**

Create `apps/api/src/usan_api/compat/kb_sources.py`:

```python
"""Shared KB text-source persistence used by BOTH the RetellAI-compat kb_service and
the native admin knowledge-bases router. Single source of truth for how a text source
is stored and how adding one (re)triggers ingestion — the two surfaces cannot drift."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.repositories import knowledge_bases as repo


@dataclass(frozen=True)
class TextSource:
    title: str
    text: str


def _content_url(source_id: uuid.UUID) -> str:
    # Internal reference; content lives in the DB and is not publicly served in v1.
    return f"https://knowledge-base.internal/source/{ids.encode_kb_source_id(source_id)}"


async def add_text_sources(
    db: AsyncSession, kb_id: uuid.UUID, texts: list[TextSource]
) -> None:
    """Persist each text source under ``kb_id`` and, when any were added, reset the KB to
    ``in_progress`` so the ingestion poller re-claims it and embeds the new sources.
    Flush-only — the caller commits. Empty ``texts`` is a no-op (KB status untouched)."""
    for t in texts:
        src = await repo.add_source(
            db, kb_id, source_type="text", title=t.title, content=t.text, content_url=""
        )
        src.content_url = _content_url(src.id)
    await db.flush()
    if texts:
        # New sources are un-chunked; returning the KB to in_progress re-enters the claim.
        await repo.mark_in_progress(db, kb_id)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/test_kb_sources_helper.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Refactor the compat `kb_service` to use the helper**

In `apps/api/src/usan_api/compat/services/kb_service.py`:

Add the helper import alongside the existing imports:

```python
from usan_api.compat.kb_sources import TextSource, add_text_sources
```

Delete the now-duplicated `_content_url` function (the `def _content_url(...)` block) and the `_persist_texts` function (the `async def _persist_texts(...)` block).

Change `create_kb` to call the helper:

```python
async def create_kb(db: AsyncSession, parsed: ParsedKbCreate) -> KnowledgeBase:
    _validate_create(parsed)
    kb = await repo.create_kb(
        db,
        name=parsed.name,
        max_chunk_size=parsed.max_chunk_size,
        min_chunk_size=parsed.min_chunk_size,
        enable_auto_refresh=parsed.enable_auto_refresh,
    )
    await add_text_sources(db, kb.id, [TextSource(title=t.title, text=t.text) for t in parsed.texts])
    await db.commit()
    return kb
```

Change `add_sources` to call the helper (drop the now-redundant explicit `mark_in_progress`, which the helper performs):

```python
async def add_sources(
    db: AsyncSession, kb_id_token: str, parsed: ParsedKbAddSources
) -> KnowledgeBase:
    _reject_unsupported_sources(parsed.has_files, parsed.has_urls)
    kb_id = ids.decode_kb_id(kb_id_token)
    kb = await repo.get_kb(db, kb_id)
    if kb is None:
        raise CompatError(404, "knowledge base not found")
    org_id = kb.organization_id
    await add_text_sources(db, kb_id, [TextSource(title=t.title, text=t.text) for t in parsed.texts])
    await db.commit()
    # In prod the get_compat_db after_begin listener re-applies app.current_org on the new
    # post-commit transaction; this explicit re-apply is for the app_session-based service
    # tests (no listener) and is harmless defense-in-depth in prod.
    await set_tenant_context(db, org_id)
    kb2 = await repo.get_kb(db, kb_id)
    assert kb2 is not None
    return kb2
```

The `KbTextInput` import in `kb_service.py` may become unused after removing `_persist_texts` — remove it from the `from usan_api.compat.schemas.knowledge_bases import (...)` list if `ruff` flags it as unused (keep `ParsedKbAddSources`, `ParsedKbCreate`).

- [ ] **Step 6: Run the full compat KB suite + lint + types**

Run:
```bash
cd apps/api && uv run pytest tests/test_kb_sources_helper.py tests/ -k "knowledge or kb" -q
uv run ruff check src/usan_api/compat/kb_sources.py src/usan_api/compat/services/kb_service.py tests/test_kb_sources_helper.py
uv run ruff format src/usan_api/compat/kb_sources.py src/usan_api/compat/services/kb_service.py tests/test_kb_sources_helper.py
uv run mypy src/usan_api/compat/kb_sources.py src/usan_api/compat/services/kb_service.py
```
Expected: all KB tests PASS (compat behavior unchanged), ruff clean, mypy clean.

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/compat/kb_sources.py apps/api/src/usan_api/compat/services/kb_service.py apps/api/tests/test_kb_sources_helper.py
git commit -m "refactor(api): extract shared add_text_sources KB helper"
```

---

### Task 2: Native `/v1/admin/knowledge-bases` schemas + router + wiring + tests

The native admin API: list/create/get/delete KB and add/delete text source, RLS-scoped, role-gated, raw-UUID schemas, reusing the repo + the Task 1 helper. Per-source status is **derived** (a source with no chunks yet is `pending`, otherwise `embedded`) since the sources table has no status column.

**Files:**
- Create: `apps/api/src/usan_api/schemas/admin_knowledge_bases.py`
- Create: `apps/api/src/usan_api/routers/admin_knowledge_bases.py`
- Modify: `apps/api/src/usan_api/main.py` (import + `include_router`)
- Test: `apps/api/tests/test_admin_knowledge_bases_api.py` (new)

**Interfaces:**
- Consumes (Task 1): `usan_api.compat.kb_sources.TextSource`, `add_text_sources`.
- Consumes (existing): `repositories.knowledge_bases` (`create_kb`, `get_kb`, `list_kbs`, `delete_kb`, `get_sources`, `get_sources_for_kbs`, `get_unchunked_sources`, `get_source`, `delete_source`); `auth.get_tenant_db`, `auth.require_admin_role`, `auth.require_admin_session`; `admin_actor.get_actor_email`; `repositories.admin_audit.record`; `db.base.AdminRole`.
- Produces: router mounted at `/v1/admin/knowledge-bases` with GET `""`, POST `""`, GET `/{kb_id}`, DELETE `/{kb_id}`, POST `/{kb_id}/sources`, DELETE `/{kb_id}/sources/{source_id}`. Response models `KbSummary`, `KbDetail`, `KbSourceOut`; request models `KbCreate`, `KbSourceCreate`.

- [ ] **Step 1: Write the schemas**

Create `apps/api/src/usan_api/schemas/admin_knowledge_bases.py`:

```python
"""Native admin knowledge-base schemas — raw UUIDs on the session-cookie/RLS plane,
distinct from the RetellAI-compat kb_-token surface. Source ``content`` is never echoed."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

NAME_MAX = 40
TITLE_MAX = 200


class KbSummary(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    source_count: int
    updated_at: datetime


class KbSourceOut(BaseModel):
    id: uuid.UUID
    title: str | None
    status: str  # derived: "pending" (no chunks yet) | "embedded"
    created_at: datetime


class KbDetail(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    error_detail: str | None
    sources: list[KbSourceOut]
    created_at: datetime
    updated_at: datetime


class KbCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > NAME_MAX:
            raise ValueError(f"name must be 1..{NAME_MAX} characters")
        return v


class KbSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    text: str

    @field_validator("title")
    @classmethod
    def _validate_title(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > TITLE_MAX:
            raise ValueError(f"title must be 1..{TITLE_MAX} characters")
        return v

    @field_validator("text")
    @classmethod
    def _validate_text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be empty")
        return v
```

- [ ] **Step 2: Write the failing router tests**

Create `apps/api/tests/test_admin_knowledge_bases_api.py`:

```python
"""Native /v1/admin/knowledge-bases API: CRUD, role gating, source lifecycle, RLS isolation."""

import asyncio

from tests.kb_helpers import _seed_kb_for_org
from tests.test_rls_p2_isolation import (  # noqa: F401 (fixtures discovered by pytest)
    _act_as_cookie,
    isolation_client,
)


def test_create_then_get_detail(client, admin_session):
    r = client.post("/v1/admin/knowledge-bases", json={"name": "Wellness FAQ"})
    assert r.status_code == 201, r.text
    kb = r.json()
    assert kb["name"] == "Wellness FAQ"
    assert kb["status"] == "in_progress"
    assert kb["sources"] == []
    kb_id = kb["id"]

    detail = client.get(f"/v1/admin/knowledge-bases/{kb_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == kb_id


def test_list_reports_source_count(client, admin_session):
    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).json()["id"]
    client.post(
        f"/v1/admin/knowledge-bases/{kb_id}/sources",
        json={"title": "Doc", "text": "hello"},
    )
    lst = client.get("/v1/admin/knowledge-bases")
    assert lst.status_code == 200
    row = next(k for k in lst.json() if k["id"] == kb_id)
    assert row["source_count"] == 1


def test_add_source_resets_to_in_progress_and_lists_pending(client, admin_session):
    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).json()["id"]
    r = client.post(
        f"/v1/admin/knowledge-bases/{kb_id}/sources",
        json={"title": "Doc A", "text": "the content"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "in_progress"
    assert len(body["sources"]) == 1
    assert body["sources"][0]["title"] == "Doc A"
    assert body["sources"][0]["status"] == "pending"  # no chunks yet
    assert "content" not in body["sources"][0]  # raw text never echoed


def test_empty_source_title_422(client, admin_session):
    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).json()["id"]
    r = client.post(
        f"/v1/admin/knowledge-bases/{kb_id}/sources",
        json={"title": "   ", "text": "x"},
    )
    assert r.status_code == 422


def test_delete_source_then_delete_kb(client, admin_session):
    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).json()["id"]
    sid = client.post(
        f"/v1/admin/knowledge-bases/{kb_id}/sources",
        json={"title": "Doc", "text": "x"},
    ).json()["sources"][0]["id"]
    assert (
        client.delete(f"/v1/admin/knowledge-bases/{kb_id}/sources/{sid}").status_code == 204
    )
    assert client.delete(f"/v1/admin/knowledge-bases/{kb_id}").status_code == 204
    assert client.get(f"/v1/admin/knowledge-bases/{kb_id}").status_code == 404


def test_get_unknown_kb_404(client, admin_session):
    assert (
        client.get(
            "/v1/admin/knowledge-bases/00000000-0000-0000-0000-000000000000"
        ).status_code
        == 404
    )


def test_create_requires_session(client):
    assert client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).status_code == 401


def test_viewer_cannot_create(client, async_database_url):
    # Reuse the viewer-cookie helper pattern from test_admin_contacts_crud_api.py.
    from tests.test_admin_contacts_crud_api import _viewer_cookie

    _viewer_cookie(client, async_database_url, email="viewer-kb@example.com")
    assert client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).status_code == 403
    # ...but a viewer CAN read.
    assert client.get("/v1/admin/knowledge-bases").status_code == 200


def test_cross_org_kb_is_404(isolation_client, two_orgs):  # noqa: F811
    client, super_url = isolation_client
    org_a, org_b = two_orgs
    from tests.test_rls_p2_isolation import _seed_super_admin

    asyncio.run(_seed_super_admin(super_url, "staff@usan.com"))
    kb_b = asyncio.run(_seed_kb_for_org(super_url, org_b, "Org B KB"))
    try:
        # Acting-as org A, org B's KB must be invisible (404, not 200/500).
        r = client.get(
            f"/v1/admin/knowledge-bases/{kb_b}",
            cookies=_act_as_cookie("staff@usan.com", org_a),
        )
        assert r.status_code == 404
        lst = client.get(
            "/v1/admin/knowledge-bases",
            cookies=_act_as_cookie("staff@usan.com", org_a),
        )
        assert str(kb_b) not in {k["id"] for k in lst.json()}
    finally:
        from tests.kb_helpers import _delete_kbs_for_org

        asyncio.run(_delete_kbs_for_org(super_url, org_b))
```

> Note: verify `_seed_super_admin` exists in `test_rls_p2_isolation.py` (it is referenced by the profiles isolation test in `test_admin_orchestration_rls.py`); if its name differs, match the actual seeding helper used there. The `isolation_client` fixture yields `(client, super_async_url)` and depends on `two_orgs` — both are already defined in that module / conftest.

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/api && uv run pytest -n0 tests/test_admin_knowledge_bases_api.py -q`
Expected: FAIL — 404s on every route (router not mounted yet).

- [ ] **Step 4: Write the router**

Create `apps/api/src/usan_api/routers/admin_knowledge_bases.py`:

```python
"""Native admin knowledge-bases API (text-only v1). RLS-scoped, org-admin self-service:
router-level session gate allows any VIEWER+; writes add require_admin_role(ADMIN). Raw
UUIDs (not compat kb_ tokens). Reuses the KB repo + the shared add_text_sources helper so
ingestion (the running poller) needs no extra wiring. Source content is never echoed."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
from usan_api.compat.kb_sources import TextSource, add_text_sources
from usan_api.db.base import AdminRole
from usan_api.db.models import KnowledgeBase
from usan_api.repositories import admin_audit
from usan_api.repositories import knowledge_bases as repo
from usan_api.schemas.admin_knowledge_bases import (
    KbCreate,
    KbDetail,
    KbSourceCreate,
    KbSourceOut,
    KbSummary,
)

# Native KB defaults — mirror ParsedKbCreate's compat defaults so a KB created here
# chunks identically to a compat KB.
_DEFAULT_MAX_CHUNK = 2000
_DEFAULT_MIN_CHUNK = 400

router = APIRouter(
    prefix="/v1/admin/knowledge-bases",
    tags=["admin-knowledge-bases"],
    dependencies=[Depends(require_admin_session)],
)


async def _detail(db: AsyncSession, kb: KnowledgeBase) -> KbDetail:
    sources = await repo.get_sources(db, kb.id)
    unchunked = {s.id for s in await repo.get_unchunked_sources(db, kb.id)}
    return KbDetail(
        id=kb.id,
        name=kb.name,
        status=kb.status,
        error_detail=kb.error_detail,
        sources=[
            KbSourceOut(
                id=s.id,
                title=s.title,
                status="pending" if s.id in unchunked else "embedded",
                created_at=s.created_at,
            )
            for s in sources
        ],
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


@router.get("", response_model=list[KbSummary])
async def list_knowledge_bases(db: AsyncSession = Depends(get_tenant_db)) -> list[KbSummary]:
    kbs = await repo.list_kbs(db)
    by_kb = await repo.get_sources_for_kbs(db, [k.id for k in kbs])
    return [
        KbSummary(
            id=k.id,
            name=k.name,
            status=k.status,
            source_count=len(by_kb.get(k.id, [])),
            updated_at=k.updated_at,
        )
        for k in kbs
    ]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=KbDetail)
async def create_knowledge_base(
    body: KbCreate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> KbDetail:
    kb = await repo.create_kb(
        db,
        name=body.name,
        max_chunk_size=_DEFAULT_MAX_CHUNK,
        min_chunk_size=_DEFAULT_MIN_CHUNK,
        enable_auto_refresh=False,
    )
    await admin_audit.record(
        db,
        actor_email=actor,
        action="knowledge_base.create",
        entity_type="knowledge_base",
        entity_id=str(kb.id),
    )
    await db.commit()
    await db.refresh(kb)
    return await _detail(db, kb)


@router.get("/{kb_id}", response_model=KbDetail)
async def get_knowledge_base(
    kb_id: uuid.UUID, db: AsyncSession = Depends(get_tenant_db)
) -> KbDetail:
    kb = await repo.get_kb(db, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return await _detail(db, kb)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    if not await repo.delete_kb(db, kb_id):
        raise HTTPException(status_code=404, detail="knowledge base not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="knowledge_base.delete",
        entity_type="knowledge_base",
        entity_id=str(kb_id),
    )
    await db.commit()


@router.post(
    "/{kb_id}/sources", status_code=status.HTTP_201_CREATED, response_model=KbDetail
)
async def add_knowledge_base_source(
    kb_id: uuid.UUID,
    body: KbSourceCreate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> KbDetail:
    kb = await repo.get_kb(db, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    await add_text_sources(db, kb_id, [TextSource(title=body.title, text=body.text)])
    await admin_audit.record(
        db,
        actor_email=actor,
        action="knowledge_base.add_source",
        entity_type="knowledge_base",
        entity_id=str(kb_id),
    )
    await db.commit()
    kb = await repo.get_kb(db, kb_id)  # re-read: status is now in_progress
    assert kb is not None
    return await _detail(db, kb)


@router.delete(
    "/{kb_id}/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_knowledge_base_source(
    kb_id: uuid.UUID,
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    if await repo.get_source(db, kb_id, source_id) is None:
        raise HTTPException(status_code=404, detail="source not found")
    await repo.delete_source(db, source_id)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="knowledge_base.delete_source",
        entity_type="knowledge_base",
        entity_id=str(kb_id),
    )
    await db.commit()
```

- [ ] **Step 5: Wire the router into the app**

In `apps/api/src/usan_api/main.py`, add `admin_knowledge_bases` to the routers import block (the `from usan_api.routers import (...)` that lists `admin_contacts`, `admin_dnc`, etc. — insert alphabetically near the other `admin_*` names), and add the include next to the other admin routers (after line 259 `app.include_router(admin_contacts.router)`):

```python
    app.include_router(admin_knowledge_bases.router)
```

- [ ] **Step 6: Run the router tests + lint + types**

Run:
```bash
cd apps/api && uv run pytest -n0 tests/test_admin_knowledge_bases_api.py -q
uv run ruff check src/usan_api/routers/admin_knowledge_bases.py src/usan_api/schemas/admin_knowledge_bases.py tests/test_admin_knowledge_bases_api.py
uv run ruff format src/usan_api/routers/admin_knowledge_bases.py src/usan_api/schemas/admin_knowledge_bases.py tests/test_admin_knowledge_bases_api.py
uv run mypy src/usan_api/routers/admin_knowledge_bases.py src/usan_api/schemas/admin_knowledge_bases.py
```
Expected: all tests PASS, ruff clean, mypy clean. If the cross-org test errors on a fixture/helper name, fix the import per the Step 2 note, then re-run.

- [ ] **Step 7: Run the full api suite to confirm no regressions**

Run: `cd apps/api && uv run pytest -q`
Expected: green (same pass count as before + the new tests).

- [ ] **Step 8: Commit**

```bash
git add apps/api/src/usan_api/schemas/admin_knowledge_bases.py apps/api/src/usan_api/routers/admin_knowledge_bases.py apps/api/src/usan_api/main.py apps/api/tests/test_admin_knowledge_bases_api.py
git commit -m "feat(api): native /v1/admin/knowledge-bases router (text-only v1)"
```

---

### Task 3: admin-ui — types, hooks, list page, create dialog, route, nav

The Knowledge Bases list page under Config: visible to any signed-in member (viewers read-only, like Defaults/Variables), with a "New knowledge base" dialog for admins. List polls while any KB is `in_progress`.

**Files:**
- Modify: `apps/admin-ui/src/types/api.ts` (add KB types)
- Create: `apps/admin-ui/src/features/knowledgeBases/hooks.ts`
- Create: `apps/admin-ui/src/features/knowledgeBases/KnowledgeBasesPage.tsx`
- Create: `apps/admin-ui/src/features/knowledgeBases/KnowledgeBaseFormDialog.tsx`
- Create: `apps/admin-ui/src/features/knowledgeBases/KnowledgeBaseDetailPage.tsx` (placeholder; Task 4 fills it in)
- Modify: `apps/admin-ui/src/components/nav-icons.tsx` (add `KnowledgeIcon`)
- Modify: `apps/admin-ui/src/components/NavSidebar.tsx` (nav entry)
- Modify: `apps/admin-ui/src/routes.tsx` (routes)
- Test: `apps/admin-ui/src/test/KnowledgeBasesPage.test.tsx` (new)

**Interfaces:**
- Produces (consumed by Task 4): types `KbSummary`, `KbSourceOut`, `KbDetail`, `KbCreate`, `KbSourceCreate` in `types/api.ts`; hooks `useKnowledgeBases()`, `useKnowledgeBase()`, `useCreateKb()`, `useDeleteKb()`, `useAddSource()`, `useDeleteSource()` in `features/knowledgeBases/hooks.ts`; `statusBadge()` exported from `KnowledgeBasesPage.tsx`; routes `/knowledge-bases` and `/knowledge-bases/:id`.
- Consumes (existing): `lib/api` (`api.get/post/del`, `ApiError`), `components/ui/*` (`Table`, `Badge`, `Button`, `Dialog`, `Input`, `Spinner`), `auth/useSession` (`useIsAdmin`), `components/ui/toast` (`pushToast`), `lib/format` (`fmtDate`).

- [ ] **Step 1: Add the KB types**

Append to `apps/admin-ui/src/types/api.ts`:

```typescript
export interface KbSummary {
  id: string;
  name: string;
  status: string;
  source_count: number;
  updated_at: string;
}

export interface KbSourceOut {
  id: string;
  title: string | null;
  status: string; // "pending" | "embedded"
  created_at: string;
}

export interface KbDetail {
  id: string;
  name: string;
  status: string;
  error_detail: string | null;
  sources: KbSourceOut[];
  created_at: string;
  updated_at: string;
}

export interface KbCreate {
  name: string;
}

export interface KbSourceCreate {
  title: string;
  text: string;
}
```

- [ ] **Step 2: Write the hooks**

Create `apps/admin-ui/src/features/knowledgeBases/hooks.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { KbCreate, KbDetail, KbSourceCreate, KbSummary } from "../../types/api";

const KB_KEY = ["knowledge-bases"] as const;

// Poll while any KB is mid-ingestion so its badge flips to complete without a manual
// refresh; stop polling once nothing is in flight.
function listRefetchInterval(rows: KbSummary[] | undefined): number | false {
  return rows?.some((k) => k.status === "in_progress") ? 3000 : false;
}

export function useKnowledgeBases() {
  return useQuery<KbSummary[]>({
    queryKey: KB_KEY,
    queryFn: () => api.get<KbSummary[]>("/v1/admin/knowledge-bases"),
    refetchInterval: (query) => listRefetchInterval(query.state.data),
  });
}

export function useKnowledgeBase(id: string) {
  return useQuery<KbDetail>({
    queryKey: [...KB_KEY, "detail", id],
    queryFn: () => api.get<KbDetail>(`/v1/admin/knowledge-bases/${id}`),
    refetchInterval: (query) => (query.state.data?.status === "in_progress" ? 3000 : false),
  });
}

export function useCreateKb() {
  const qc = useQueryClient();
  return useMutation<KbDetail, ApiError, KbCreate>({
    mutationFn: (body) => api.post<KbDetail>("/v1/admin/knowledge-bases", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: KB_KEY });
    },
    // 422 (invalid name) surfaces inline in the dialog, so no toast here.
  });
}

export function useDeleteKb() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/knowledge-bases/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: KB_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useAddSource(kbId: string) {
  const qc = useQueryClient();
  return useMutation<KbDetail, ApiError, KbSourceCreate>({
    mutationFn: (body) => api.post<KbDetail>(`/v1/admin/knowledge-bases/${kbId}/sources`, body),
    onSuccess: (data) => {
      qc.setQueryData([...KB_KEY, "detail", kbId], data);
      void qc.invalidateQueries({ queryKey: KB_KEY });
    },
  });
}

export function useDeleteSource(kbId: string) {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (sourceId) =>
      api.del<void>(`/v1/admin/knowledge-bases/${kbId}/sources/${sourceId}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...KB_KEY, "detail", kbId] });
      void qc.invalidateQueries({ queryKey: KB_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}
```

> `useKnowledgeBase`, `useAddSource`, `useDeleteSource` are used by Task 4; defining them here keeps all KB data access in one hooks module (DRY).

- [ ] **Step 3: Add the nav icon**

In `apps/admin-ui/src/components/nav-icons.tsx`, add (following the existing `Icon`-wrapper convention, Lucide "library" glyph):

```typescript
// Config → Knowledge Bases (stacked books / library).
export function KnowledgeIcon() {
  return (
    <Icon>
      <path d="m16 6 4 14" />
      <path d="M12 6v14" />
      <path d="M8 8v12" />
      <path d="M4 4v16" />
    </Icon>
  );
}
```

- [ ] **Step 4: Add the nav entry**

In `apps/admin-ui/src/components/NavSidebar.tsx`, add `KnowledgeIcon` to the `nav-icons` import list, and add this item to the **Config** group's `items` array (after Variables — not `adminOnly`, since viewers may read):

```typescript
      { to: "/knowledge-bases", label: "Knowledge", icon: KnowledgeIcon },
```

- [ ] **Step 5: Write the create dialog**

Create `apps/admin-ui/src/features/knowledgeBases/KnowledgeBaseFormDialog.tsx`:

```typescript
import { useState, type FormEvent } from "react";
import { Dialog } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import type { ApiError } from "../../lib/api";
import { useCreateKb } from "./hooks";

interface Props {
  onClose: () => void;
  onCreated: (id: string) => void;
}

// Hand-rolled useState form (codebase convention — no RHF). Name only for v1.
export function KnowledgeBaseFormDialog({ onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const create = useCreateKb();
  const serverError = (create.error as ApiError | null)?.detail;

  function handleSubmit(e: FormEvent): void {
    e.preventDefault();
    setLocalError(null);
    const trimmed = name.trim();
    if (trimmed.length === 0) {
      setLocalError("Name is required.");
      return;
    }
    create.mutate({ name: trimmed }, { onSuccess: (kb) => onCreated(kb.id) });
  }

  return (
    <Dialog open onClose={onClose} title="New knowledge base">
      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="kb-name">
            Name
          </label>
          <Input id="kb-name" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
        {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={create.isPending}>
            Cancel
          </Button>
          <Button type="submit" disabled={create.isPending}>
            {create.isPending ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
```

- [ ] **Step 6: Write the list page (exports `statusBadge`)**

Create `apps/admin-ui/src/features/knowledgeBases/KnowledgeBasesPage.tsx`:

```typescript
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { fmtDate } from "../../lib/format";
import { useIsAdmin } from "../../auth/useSession";
import { useKnowledgeBases } from "./hooks";
import { KnowledgeBaseFormDialog } from "./KnowledgeBaseFormDialog";

export function statusBadge(status: string) {
  if (status === "complete") return <Badge tone="green">complete</Badge>;
  if (status === "error") return <Badge tone="red">error</Badge>;
  return <Badge tone="amber">in progress</Badge>;
}

export function KnowledgeBasesPage() {
  const isAdmin = useIsAdmin();
  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const kbs = useKnowledgeBases();
  const list = kbs.data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl text-ink-strong">Knowledge</h1>
        {isAdmin ? <Button onClick={() => setCreating(true)}>New knowledge base</Button> : null}
      </div>

      {kbs.isLoading ? (
        <div className="flex items-center gap-2 text-muted">
          <Spinner /> Loading knowledge bases…
        </div>
      ) : kbs.isError ? (
        <p className="text-sm text-red-700">
          Failed to load knowledge bases: {(kbs.error as Error)?.message}
        </p>
      ) : (
        <Table>
          <Thead>
            <Tr>
              <Th>Name</Th>
              <Th>Status</Th>
              <Th>Sources</Th>
              <Th>Updated</Th>
            </Tr>
          </Thead>
          <Tbody>
            {list.length === 0 ? (
              <Tr>
                <Td className="text-faint" colSpan={4}>
                  No knowledge bases yet.
                </Td>
              </Tr>
            ) : null}
            {list.map((k) => (
              <Tr key={k.id}>
                <Td className="font-medium">
                  <Link className="text-accent hover:underline" to={`/knowledge-bases/${k.id}`}>
                    {k.name}
                  </Link>
                </Td>
                <Td>{statusBadge(k.status)}</Td>
                <Td className="tabular-nums">{k.source_count}</Td>
                <Td className="whitespace-nowrap text-xs">{fmtDate(k.updated_at)}</Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}

      {creating ? (
        <KnowledgeBaseFormDialog
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            navigate(`/knowledge-bases/${id}`);
          }}
        />
      ) : null}
    </div>
  );
}
```

> If the `Badge` component's `tone` prop does not include `"amber"`/`"red"`, open `apps/admin-ui/src/components/ui/badge.tsx` and use the nearest existing tones (e.g. the tone Schedules uses for "off", and whatever the codebase uses for warnings/errors). Do not invent a tone the component doesn't support — tsc will fail.

- [ ] **Step 7: Create the detail-page placeholder**

Create `apps/admin-ui/src/features/knowledgeBases/KnowledgeBaseDetailPage.tsx` (Task 4 replaces the body):

```typescript
export function KnowledgeBaseDetailPage() {
  return null;
}
```

- [ ] **Step 8: Add the routes**

In `apps/admin-ui/src/routes.tsx`, add the imports near the other feature imports:

```typescript
import { KnowledgeBasesPage } from "./features/knowledgeBases/KnowledgeBasesPage";
import { KnowledgeBaseDetailPage } from "./features/knowledgeBases/KnowledgeBaseDetailPage";
```

And add these two children inside the `PageLayout` children array (not wrapped in `RequireAdmin` — viewers read; the page hides write controls):

```typescript
          { path: "knowledge-bases", element: <KnowledgeBasesPage /> },
          { path: "knowledge-bases/:id", element: <KnowledgeBaseDetailPage /> },
```

- [ ] **Step 9: Write the list page test**

Create `apps/admin-ui/src/test/KnowledgeBasesPage.test.tsx`:

```typescript
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { KbSummary } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b: unknown) => postMock(u, b),
    del: vi.fn(),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { KnowledgeBasesPage } from "../features/knowledgeBases/KnowledgeBasesPage";

const rows: KbSummary[] = [
  {
    id: "aaaa1111-1111-1111-1111-111111111111",
    name: "Wellness FAQ",
    status: "complete",
    source_count: 2,
    updated_at: "2026-07-02T10:00:00Z",
  },
];

function renderPage(role: "admin" | "viewer") {
  getMock.mockImplementation((url: string) => {
    if (url === "/v1/auth/me") return Promise.resolve(meFixture(role));
    if (url === "/v1/admin/knowledge-bases") return Promise.resolve(rows);
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/knowledge-bases"]}>
        <Routes>
          <Route path="/knowledge-bases" element={<KnowledgeBasesPage />} />
          <Route path="/knowledge-bases/:id" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("KnowledgeBasesPage", () => {
  it("lists knowledge bases with a status badge", async () => {
    renderPage("admin");
    expect(await screen.findByText("Wellness FAQ")).toBeInTheDocument();
    expect(screen.getByText("complete")).toBeInTheDocument();
  });

  it("shows the New button to admins and creates a KB", async () => {
    postMock.mockResolvedValue({
      id: "bbbb2222-2222-2222-2222-222222222222",
      name: "New KB",
      status: "in_progress",
      error_detail: null,
      sources: [],
      created_at: "2026-07-02T10:00:00Z",
      updated_at: "2026-07-02T10:00:00Z",
    });
    renderPage("admin");
    await screen.findByText("Wellness FAQ");
    await userEvent.click(screen.getByRole("button", { name: "New knowledge base" }));
    await userEvent.type(screen.getByLabelText("Name"), "New KB");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/knowledge-bases", { name: "New KB" }),
    );
  });

  it("hides the New button from viewers", async () => {
    renderPage("viewer");
    await screen.findByText("Wellness FAQ");
    expect(screen.queryByRole("button", { name: "New knowledge base" })).toBeNull();
  });
});
```

> `screen.getByLabelText("Name")` relies on the `<label htmlFor="kb-name">Name</label>` + `<Input id="kb-name">` association from Step 5. The Contacts form relies on the same association, so `Input` forwards `id`. If `meFixture` takes different arguments than `"admin"`/`"viewer"`, match its actual signature (see `src/test/meFixture.ts`).

- [ ] **Step 10: Run frontend tests + typecheck**

Run:
```bash
cd apps/admin-ui && npx vitest run src/test/KnowledgeBasesPage.test.tsx
npm run typecheck
```
Expected: 3 tests PASS, tsc clean. (Per the admin-ui-test-flakiness note, if an unrelated test times out under load, re-run in isolation — only this file matters here.)

- [ ] **Step 11: Commit**

```bash
git add apps/admin-ui/src/types/api.ts apps/admin-ui/src/features/knowledgeBases/ apps/admin-ui/src/components/nav-icons.tsx apps/admin-ui/src/components/NavSidebar.tsx apps/admin-ui/src/routes.tsx apps/admin-ui/src/test/KnowledgeBasesPage.test.tsx
git commit -m "feat(admin-ui): knowledge bases list + create page"
```

---

### Task 4: admin-ui — KB detail page (sources list, add-source, delete, polling)

The detail view: KB name + status, its sources (title, derived status, admin delete), an "Add text source" form (admin only), and live polling while `in_progress`. Viewers see a read-only list.

**Files:**
- Modify: `apps/admin-ui/src/features/knowledgeBases/KnowledgeBaseDetailPage.tsx` (replace the Task 3 placeholder)
- Test: `apps/admin-ui/src/test/KnowledgeBaseDetailPage.test.tsx` (new)

**Interfaces:**
- Consumes (Task 3): `useKnowledgeBase(id)`, `useAddSource(id)`, `useDeleteSource(id)`, `useDeleteKb()`, `statusBadge` (import from `KnowledgeBasesPage`).
- Consumes (existing): `useParams`/`useNavigate`, `useIsAdmin`, `components/ui/*` (`Input`, `Textarea`, `Button`, `Badge`, `Spinner`), `ConfirmDialog` (`components/ConfirmDialog`), `lib/format.fmtDate`.

- [ ] **Step 1: Write the failing detail test**

Create `apps/admin-ui/src/test/KnowledgeBaseDetailPage.test.tsx`:

```typescript
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { KbDetail } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const postMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b: unknown) => postMock(u, b),
    del: (u: string) => delMock(u),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { KnowledgeBaseDetailPage } from "../features/knowledgeBases/KnowledgeBaseDetailPage";

const KB_ID = "aaaa1111-1111-1111-1111-111111111111";
const detail: KbDetail = {
  id: KB_ID,
  name: "Wellness FAQ",
  status: "complete",
  error_detail: null,
  sources: [
    {
      id: "cccc3333-3333-3333-3333-333333333333",
      title: "Intro",
      status: "embedded",
      created_at: "2026-07-02T10:00:00Z",
    },
  ],
  created_at: "2026-07-02T09:00:00Z",
  updated_at: "2026-07-02T10:00:00Z",
};

function renderDetail(role: "admin" | "viewer") {
  getMock.mockImplementation((url: string) => {
    if (url === "/v1/auth/me") return Promise.resolve(meFixture(role));
    if (url === `/v1/admin/knowledge-bases/${KB_ID}`) return Promise.resolve(detail);
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/knowledge-bases/${KB_ID}`]}>
        <Routes>
          <Route path="/knowledge-bases/:id" element={<KnowledgeBaseDetailPage />} />
          <Route path="/knowledge-bases" element={<div>list</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("KnowledgeBaseDetailPage", () => {
  it("renders the KB and its sources", async () => {
    renderDetail("admin");
    expect(await screen.findByText("Wellness FAQ")).toBeInTheDocument();
    expect(screen.getByText("Intro")).toBeInTheDocument();
  });

  it("adds a text source", async () => {
    postMock.mockResolvedValue({ ...detail, status: "in_progress" });
    renderDetail("admin");
    await screen.findByText("Wellness FAQ");
    await userEvent.type(screen.getByLabelText("Title"), "New doc");
    await userEvent.type(screen.getByLabelText("Text"), "some content");
    await userEvent.click(screen.getByRole("button", { name: "Add source" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(`/v1/admin/knowledge-bases/${KB_ID}/sources`, {
        title: "New doc",
        text: "some content",
      }),
    );
  });

  it("hides write controls from viewers", async () => {
    renderDetail("viewer");
    await screen.findByText("Wellness FAQ");
    expect(screen.queryByRole("button", { name: "Add source" })).toBeNull();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/admin-ui && npx vitest run src/test/KnowledgeBaseDetailPage.test.tsx`
Expected: FAIL — the placeholder renders `null`, so "Wellness FAQ" is never found.

- [ ] **Step 3: Implement the detail page**

Replace the contents of `apps/admin-ui/src/features/knowledgeBases/KnowledgeBaseDetailPage.tsx`:

```typescript
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { fmtDate } from "../../lib/format";
import { useIsAdmin } from "../../auth/useSession";
import type { ApiError } from "../../lib/api";
import { statusBadge } from "./KnowledgeBasesPage";
import { useAddSource, useDeleteKb, useDeleteSource, useKnowledgeBase } from "./hooks";

function sourceBadge(status: string) {
  return status === "embedded" ? (
    <Badge tone="green">embedded</Badge>
  ) : (
    <Badge tone="amber">pending</Badge>
  );
}

export function KnowledgeBaseDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const isAdmin = useIsAdmin();
  const kb = useKnowledgeBase(id);
  const addSource = useAddSource(id);
  const deleteSource = useDeleteSource(id);
  const deleteKb = useDeleteKb();

  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [confirmDeleteKb, setConfirmDeleteKb] = useState(false);
  const serverError = (addSource.error as ApiError | null)?.detail;

  function handleAdd(e: FormEvent): void {
    e.preventDefault();
    setLocalError(null);
    if (title.trim().length === 0) {
      setLocalError("Title is required.");
      return;
    }
    if (text.trim().length === 0) {
      setLocalError("Text is required.");
      return;
    }
    addSource.mutate(
      { title: title.trim(), text: text.trim() },
      {
        onSuccess: () => {
          setTitle("");
          setText("");
        },
      },
    );
  }

  if (kb.isLoading) {
    return (
      <div className="flex items-center gap-2 text-muted">
        <Spinner /> Loading…
      </div>
    );
  }
  if (kb.isError || !kb.data) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-red-700">Knowledge base not found.</p>
        <Link className="text-accent hover:underline" to="/knowledge-bases">
          Back to knowledge bases
        </Link>
      </div>
    );
  }

  const data = kb.data;

  return (
    <div className="space-y-5">
      <div>
        <Link className="text-xs text-muted hover:underline" to="/knowledge-bases">
          ← Knowledge
        </Link>
        <div className="mt-1 flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-display text-2xl text-ink-strong">{data.name}</h1>
          <div className="flex items-center gap-3">
            {statusBadge(data.status)}
            {isAdmin ? (
              <Button variant="secondary" onClick={() => setConfirmDeleteKb(true)}>
                Delete
              </Button>
            ) : null}
          </div>
        </div>
        {data.status === "error" && data.error_detail ? (
          <p className="mt-2 text-sm text-red-700">Ingestion error: {data.error_detail}</p>
        ) : null}
      </div>

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-ink-strong">Sources</h2>
        <Table>
          <Thead>
            <Tr>
              <Th>Title</Th>
              <Th>Status</Th>
              <Th>Added</Th>
              {isAdmin ? <Th> </Th> : null}
            </Tr>
          </Thead>
          <Tbody>
            {data.sources.length === 0 ? (
              <Tr>
                <Td className="text-faint" colSpan={isAdmin ? 4 : 3}>
                  No sources yet.
                </Td>
              </Tr>
            ) : null}
            {data.sources.map((s) => (
              <Tr key={s.id}>
                <Td className="font-medium">{s.title ?? "—"}</Td>
                <Td>{sourceBadge(s.status)}</Td>
                <Td className="whitespace-nowrap text-xs">{fmtDate(s.created_at)}</Td>
                {isAdmin ? (
                  <Td>
                    <Button
                      variant="secondary"
                      onClick={() => deleteSource.mutate(s.id)}
                      disabled={deleteSource.isPending}
                    >
                      Remove
                    </Button>
                  </Td>
                ) : null}
              </Tr>
            ))}
          </Tbody>
        </Table>
      </section>

      {isAdmin ? (
        <section className="max-w-xl space-y-2">
          <h2 className="text-sm font-semibold text-ink-strong">Add text source</h2>
          <form onSubmit={handleAdd} className="space-y-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="kb-src-title">
                Title
              </label>
              <Input id="kb-src-title" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="kb-src-text">
                Text
              </label>
              <Textarea
                id="kb-src-text"
                rows={6}
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
            </div>
            {localError ? <p className="text-xs font-medium text-red-700">{localError}</p> : null}
            {serverError ? <p className="text-xs font-medium text-red-700">{serverError}</p> : null}
            <div className="flex justify-end">
              <Button type="submit" disabled={addSource.isPending}>
                {addSource.isPending ? "Adding…" : "Add source"}
              </Button>
            </div>
          </form>
        </section>
      ) : null}

      {confirmDeleteKb ? (
        <ConfirmDialog
          title="Delete knowledge base?"
          message={`This permanently deletes "${data.name}" and all its sources.`}
          confirmLabel="Delete"
          onCancel={() => setConfirmDeleteKb(false)}
          onConfirm={() => {
            deleteKb.mutate(data.id, { onSuccess: () => navigate("/knowledge-bases") });
          }}
        />
      ) : null}
    </div>
  );
}
```

> `ConfirmDialog`'s prop names may differ from the above (`title`/`message`/`confirmLabel`/`onCancel`/`onConfirm`). Open `apps/admin-ui/src/components/ConfirmDialog.tsx` and match its actual prop API before running — the Contacts delete flow already uses it, so copy that call shape. Likewise confirm `Badge` supports the tones used (`green`/`amber`); if not, substitute supported tones. Confirm `Textarea` exists at `components/ui/textarea` (the Contacts form imports it from there).

- [ ] **Step 4: Run the detail test + typecheck**

Run:
```bash
cd apps/admin-ui && npx vitest run src/test/KnowledgeBaseDetailPage.test.tsx
npm run typecheck
```
Expected: 3 tests PASS, tsc clean.

- [ ] **Step 5: Run the full admin-ui suite + build**

Run:
```bash
cd apps/admin-ui && npx vitest run
npm run build
```
Expected: green (re-run any load-flaky unrelated test in isolation per the known flakiness note), and the production build succeeds.

- [ ] **Step 6: Commit**

```bash
git add apps/admin-ui/src/features/knowledgeBases/KnowledgeBaseDetailPage.tsx apps/admin-ui/src/test/KnowledgeBaseDetailPage.test.tsx
git commit -m "feat(admin-ui): knowledge base detail with text sources"
```

---

## Post-implementation

- [ ] Open a squash PR (`feat: knowledge bases admin UI (text-only v1)`) with a summary + test plan; base `main`, branch `feat/kb-admin-ui`.
- [ ] Deploys on the next `v*` tag (no migration; the ingestion poller is already enabled in prod as of v0.14.0 / secret v9, so content added via the UI ingests immediately).
- [ ] RAG activation remains a separate follow-up: bind a KB to an agent, then flip `KB_RETRIEVAL_ENABLED` / `KB_RETRIEVAL_VOICE_ENABLED` (ops step).

## Self-Review notes (verified against the spec)

- **Spec coverage:** native router (6 endpoints) → Task 2; shared helper (no drift) → Task 1; list/create/detail/add-source/delete UI + polling → Tasks 3-4; RLS isolation + role gating tests → Task 2 (backend) & viewer-gating in Tasks 3-4 (frontend); text-only enforced by the schema shape (only `{title,text}` accepted). No new migration. ✔
- **Per-source status:** the sources table has no status column (verified in `db/models.py`), so status is derived from chunk presence via `get_unchunked_sources` — noted explicitly in Task 2. The spec's `KbSourceOut` "status" is satisfied by this derivation (`pending`/`embedded`). ✔
- **Type consistency:** `KbSummary`/`KbDetail`/`KbSourceOut` fields match between backend schemas (Task 2) and TS types (Task 3); hook/endpoint URLs consistent; `statusBadge` defined once (Task 3) and imported by Task 4. ✔
- **Known-unknowns flagged inline** (fixture/helper names in `test_rls_p2_isolation`, `Badge` tones, `ConfirmDialog` prop API, `meFixture` signature) so the implementer verifies against the real component rather than trusting a guessed signature. ✔
