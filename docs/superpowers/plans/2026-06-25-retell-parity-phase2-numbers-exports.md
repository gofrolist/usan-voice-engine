# RetellAI Parity Phase 2 — Phone Numbers + Exports — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the RetellAI phone-number CRUD surface (`import`/`get`/`update`/`list`/`delete`) for real over a new per-org RLS table with persisted-but-not-yet-honored agent bindings, keep `create-phone-number` a documented-501, and serve `GET /v2/list-export-requests` as a conformant empty-list stub.

**Architecture:** A new `phone_numbers` `TenantScoped`+FORCE-RLS table (migration 0040, owner-DDL, `GRANT … TO usan_app`), a thin RLS-scoped repository, compat Pydantic schemas + serializer, and two new compat routers (`phone_numbers`, `export_requests`) registered on the mounted compat sub-app — with the served paths removed from `_UNSUPPORTED` in the same change. The raw E.164 (oracle-forced as the path param) is masked in the access log.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, Postgres RLS, pytest + the compat conformance harness (`openapi-schema-validator` against the vendored oracle + `retell-sdk==5.53.0`).

**Spec:** `docs/superpowers/specs/2026-06-25-retell-parity-phase2-numbers-exports-design.md`

## Global Constraints

- **No faked behavior.** Agent bindings are persisted + echoed but NOT honored at call-routing time; documented in route docstrings + a `docs/deployment/` note. No endpoint returns 200 implying a call routes.
- **`exclude_none` is load-bearing** (oracle omits null optionals). Single-object responses use `response_model=PhoneNumberResponse, response_model_exclude_none=True`. List/export responses are plain dicts built with no null keys.
- **Errors via `CompatError(status, message)` only** (never `HTTPException`); envelope is `{"status": <int>, "message": str}`. Malformed request bodies → 422 via the existing global `RequestValidationError` handler.
- **Not-found / cross-org → `CompatError(404, "phone number not found")`**, matching the house convention (`calls.py`/`agents.py`/`catalog.py`). This deviates from the oracle's 422 — recorded in the spec §13, NOT changed here.
- **`sip_auth_password` is write-only:** never in any response (incl. `sip_outbound_trunk_config`), never logged, never in `_audit`. `_audit` logs org + op only (no phone, no secret).
- **AgentWeight validation = structural + resolution only, NO sum-to-1.** Structural (422 via Pydantic): `agent_id` non-empty, `weight` in `(0,1]`. Resolution (422 in-handler): each `agent_id` decodes via `ids.decode_agent_id` and resolves to a non-archived org `AgentProfile`.
- **Stored webhook URLs SSRF-validated at write** via `ssrf_guard.validate_webhook_url` (raises `ValueError` → 422).
- **`ignore_e164_validation` is a `StrictBool`** (oracle: string `"true"`/`"false"` are invalid → 422). Default `True`. When explicitly `False`, an invalid E.164 → `CompatError(400, …)`.
- **`KNOWN_GAPS` stays `frozenset()`.** Each served path is removed from `_UNSUPPORTED` in the SAME change it becomes a real route; `create-phone-number` stays 501. Paths/param names stay oracle-verbatim (`{phone_number}`, `/v2/` on list only).
- **Migration 0040 is owner-DDL**, modeled on `0037` (new TenantScoped+FORCE-RLS table) **including `GRANT SELECT, INSERT, UPDATE, DELETE ON phone_numbers TO usan_app`**; `revision = "0040"`, `down_revision = "0039"`.
- **Merge-to-main only**, no `v*` tag. Commits: `type(scope): description`, scope `api`. Attribution disabled (no `Co-Authored-By`, no 🤖 footer).
- **Run tests:** `cd apps/api && uv run pytest -n0 tests/compat/test_phone_numbers_frozen.py -v` (serial for a single file). CI runs `uv run mypy` (files=`src`) + `ruff check .` — run both before finishing a task. NEVER `uv run mypy .` (pulls tests → thousands of pre-existing errors).

---

### Task 1: Migration 0040 + `PhoneNumber` ORM + truncate-list

**Files:**
- Create: `apps/api/migrations/versions/0040_phone_numbers.py`
- Modify: `apps/api/src/usan_api/db/models.py` (add `PhoneNumber` class; the file already imports `UUID`, `Text`, `JSONB`, `DateTime`, `func`, `text`, `UniqueConstraint`, `ForeignKey`, `Mapped`, `mapped_column`, `Base`, `TenantScoped` — reuse them; add `ARRAY`, `Integer` to the sqlalchemy import if missing)
- Modify: `apps/api/tests/conftest.py` (`_TRUNCATE_ALL` — add `phone_numbers`)
- Test: `apps/api/tests/test_phone_numbers_migration.py`

**Interfaces:**
- Produces: ORM `PhoneNumber(Base, TenantScoped)` with `__tablename__ = "phone_numbers"`; columns `id, organization_id, phone_e164, phone_number_type, phone_number_pretty, nickname, area_code, inbound_webhook_url, inbound_sms_webhook_url, allowed_inbound_country_list, allowed_outbound_country_list, fallback_number, transport, termination_uri, sip_auth_username, sip_auth_password, inbound_agents, outbound_agents, inbound_sms_agents, outbound_sms_agents, created_at, updated_at`; unique `(phone_e164, organization_id)` = `uq_phone_numbers_e164_org`.

- [ ] **Step 1: Write the failing test**

`apps/api/tests/test_phone_numbers_migration.py`:

```python
"""Migration 0040: phone_numbers is TenantScoped + FORCE-RLS, and usan_app can CRUD it.

Mirrors the existing compat RLS-isolation test pattern: connect as the non-superuser
usan_app role (app_session), set the tenant context, and assert a row written under org A
is invisible under org B (RLS) and that usan_app has the table grant (no permission error).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from usan_api.db.models import PhoneNumber
from usan_api.tenant_context import set_tenant_context


@pytest.mark.asyncio
async def test_phone_numbers_rls_isolation_and_grant(app_session, two_orgs) -> None:
    org_a, org_b = two_orgs

    await set_tenant_context(app_session, org_a)
    app_session.add(PhoneNumber(phone_e164="+15550001111", phone_number_type="custom"))
    await app_session.flush()  # usan_app INSERT must succeed (GRANT present)
    rows_a = (await app_session.execute(select(PhoneNumber))).scalars().all()
    assert [r.phone_e164 for r in rows_a] == ["+15550001111"]

    # Same connection, switch tenant context: RLS hides org A's row from org B.
    await set_tenant_context(app_session, org_b)
    rows_b = (await app_session.execute(select(PhoneNumber))).scalars().all()
    assert rows_b == []
```

- [ ] **Step 2: Run it — expect FAIL** (`ImportError: cannot import name 'PhoneNumber'` or `UndefinedTable: phone_numbers`).

Run: `cd apps/api && uv run pytest -n0 tests/test_phone_numbers_migration.py -v`

- [ ] **Step 3: Add the migration** `apps/api/migrations/versions/0040_phone_numbers.py`:

```python
"""phone_numbers: per-org TenantScoped + FORCE RLS table for the compat phone-number surface.

New owner-DDL table (modeled on 0037). Bindings (inbound/outbound[/_sms]_agents) are stored as
JSONB AgentWeight lists; sip_auth_password is write-only (never echoed). GRANT to usan_app so the
least-priv runtime role can CRUD it.

Revision ID: 0040
Revises: 0039
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_DEFAULT_EXPR = "COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (organization_id = current_setting('app.current_org', true)::uuid) "
        f"WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO usan_app")


def upgrade() -> None:
    op.create_table(
        "phone_numbers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("phone_e164", sa.Text(), nullable=False),
        sa.Column("phone_number_type", sa.Text(), nullable=False),
        sa.Column("phone_number_pretty", sa.Text(), nullable=True),
        sa.Column("nickname", sa.Text(), nullable=True),
        sa.Column("area_code", sa.Integer(), nullable=True),
        sa.Column("inbound_webhook_url", sa.Text(), nullable=True),
        sa.Column("inbound_sms_webhook_url", sa.Text(), nullable=True),
        sa.Column("allowed_inbound_country_list", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("allowed_outbound_country_list", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("fallback_number", sa.Text(), nullable=True),
        sa.Column("transport", sa.Text(), nullable=True),
        sa.Column("termination_uri", sa.Text(), nullable=True),
        sa.Column("sip_auth_username", sa.Text(), nullable=True),
        sa.Column("sip_auth_password", sa.Text(), nullable=True),
        sa.Column("inbound_agents", postgresql.JSONB(), nullable=True),
        sa.Column("outbound_agents", postgresql.JSONB(), nullable=True),
        sa.Column("inbound_sms_agents", postgresql.JSONB(), nullable=True),
        sa.Column("outbound_sms_agents", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone_e164", "organization_id", name="uq_phone_numbers_e164_org"),
    )
    op.create_index("ix_phone_numbers_organization_id", "phone_numbers", ["organization_id"])
    # Keyset list order (created_at, id); index it for the list endpoint.
    op.create_index("ix_phone_numbers_created_at_id", "phone_numbers", ["created_at", "id"])
    _enable_rls("phone_numbers")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON phone_numbers")
    op.drop_index("ix_phone_numbers_created_at_id", table_name="phone_numbers")
    op.drop_index("ix_phone_numbers_organization_id", table_name="phone_numbers")
    op.drop_table("phone_numbers")
```

- [ ] **Step 4: Add the ORM model** in `apps/api/src/usan_api/db/models.py` (place near `Contact`; reuse the file's existing imports). The `organization_id` comes from the `TenantScoped` mixin — do NOT redeclare it:

```python
class PhoneNumber(Base, TenantScoped):
    __tablename__ = "phone_numbers"
    __table_args__ = (
        UniqueConstraint("phone_e164", "organization_id", name="uq_phone_numbers_e164_org"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    phone_e164: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number_type: Mapped[str] = mapped_column(Text, nullable=False)
    phone_number_pretty: Mapped[str | None] = mapped_column(Text)
    nickname: Mapped[str | None] = mapped_column(Text)
    area_code: Mapped[int | None] = mapped_column(Integer)
    inbound_webhook_url: Mapped[str | None] = mapped_column(Text)
    inbound_sms_webhook_url: Mapped[str | None] = mapped_column(Text)
    allowed_inbound_country_list: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    allowed_outbound_country_list: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    fallback_number: Mapped[str | None] = mapped_column(Text)
    transport: Mapped[str | None] = mapped_column(Text)
    termination_uri: Mapped[str | None] = mapped_column(Text)
    sip_auth_username: Mapped[str | None] = mapped_column(Text)
    sip_auth_password: Mapped[str | None] = mapped_column(Text)
    inbound_agents: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    outbound_agents: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    inbound_sms_agents: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    outbound_sms_agents: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
```

(If `ARRAY` / `Integer` are not already imported in models.py, add them to the existing `from sqlalchemy import …` line. `Any` is already imported.)

- [ ] **Step 5: Add `phone_numbers` to the test truncate list** in `apps/api/tests/conftest.py` — append `phone_numbers` to the `_TRUNCATE_ALL` string (e.g. right after `compat_webhook_endpoints,`): `"compat_webhook_deliveries, compat_webhook_endpoints, phone_numbers, "`.

- [ ] **Step 6: Run the test — expect PASS.**

Run: `cd apps/api && uv run pytest -n0 tests/test_phone_numbers_migration.py -v`
Expected: PASS (the session-scoped `database_url` fixture runs `alembic upgrade head`, applying 0040).

- [ ] **Step 7: Lint + typecheck.**

Run: `cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy`
Expected: clean.

- [ ] **Step 8: Commit.**

```bash
git add apps/api/migrations/versions/0040_phone_numbers.py apps/api/src/usan_api/db/models.py apps/api/tests/conftest.py apps/api/tests/test_phone_numbers_migration.py
git commit -m "feat(api): add phone_numbers table (migration 0040) + PhoneNumber ORM"
```

---

### Task 2: `phone_numbers` repository

**Files:**
- Create: `apps/api/src/usan_api/repositories/phone_numbers.py`
- Test: `apps/api/tests/test_phone_numbers_repo.py`

**Interfaces:**
- Consumes: `PhoneNumber` (Task 1).
- Produces:
  - `async create_phone_number(db, *, phone_e164, phone_number_type, nickname=None, area_code=None, inbound_webhook_url=None, inbound_sms_webhook_url=None, allowed_inbound_country_list=None, allowed_outbound_country_list=None, fallback_number=None, transport=None, termination_uri=None, sip_auth_username=None, sip_auth_password=None, inbound_agents=None, outbound_agents=None, inbound_sms_agents=None, outbound_sms_agents=None) -> PhoneNumber`
  - `async get_by_e164(db, phone_e164: str) -> PhoneNumber | None`
  - `async update_by_e164(db, phone_e164: str, fields: dict[str, Any]) -> PhoneNumber | None`
  - `async delete_by_e164(db, phone_e164: str) -> bool`
  - `async list_phone_numbers(db, *, limit: int, descending: bool, after_id: uuid.UUID | None) -> list[PhoneNumber]`

- [ ] **Step 1: Write the failing test** `apps/api/tests/test_phone_numbers_repo.py`:

```python
"""phone_numbers repository: CRUD + keyset list under a single org context (usan_app)."""

from __future__ import annotations

import pytest

from usan_api.db.models import PhoneNumber
from usan_api.repositories import phone_numbers as repo
from usan_api.tenant_context import set_tenant_context


@pytest.mark.asyncio
async def test_crud_and_keyset_list(app_session, two_orgs) -> None:
    org_a, _ = two_orgs
    await set_tenant_context(app_session, org_a)

    a = await repo.create_phone_number(
        app_session, phone_e164="+15550000001", phone_number_type="custom", nickname="one"
    )
    await repo.create_phone_number(
        app_session, phone_e164="+15550000002", phone_number_type="custom"
    )
    assert isinstance(a, PhoneNumber)

    got = await repo.get_by_e164(app_session, "+15550000001")
    assert got is not None and got.nickname == "one"
    assert await repo.get_by_e164(app_session, "+19999999999") is None

    updated = await repo.update_by_e164(app_session, "+15550000001", {"nickname": "renamed"})
    assert updated is not None and updated.nickname == "renamed"

    page = await repo.list_phone_numbers(app_session, limit=10, descending=True, after_id=None)
    assert {p.phone_e164 for p in page} == {"+15550000001", "+15550000002"}

    # keyset: page after the newest row excludes it
    newest = page[0]
    after = await repo.list_phone_numbers(
        app_session, limit=10, descending=True, after_id=newest.id
    )
    assert newest.id not in {p.id for p in after}

    assert await repo.delete_by_e164(app_session, "+15550000002") is True
    assert await repo.delete_by_e164(app_session, "+15550000002") is False
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError: usan_api.repositories.phone_numbers`).

Run: `cd apps/api && uv run pytest -n0 tests/test_phone_numbers_repo.py -v`

- [ ] **Step 3: Implement the repository** `apps/api/src/usan_api/repositories/phone_numbers.py`:

```python
import uuid
from typing import Any, cast

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import PhoneNumber

# Column allow-list for update_by_e164 (mass-assignment guard): id, organization_id,
# phone_e164, phone_number_type, created_at/updated_at are never writable via update.
_UPDATABLE_FIELDS = frozenset(
    {
        "nickname",
        "inbound_webhook_url",
        "inbound_sms_webhook_url",
        "allowed_inbound_country_list",
        "allowed_outbound_country_list",
        "fallback_number",
        "transport",
        "termination_uri",
        "sip_auth_username",
        "sip_auth_password",
        "inbound_agents",
        "outbound_agents",
        "inbound_sms_agents",
        "outbound_sms_agents",
    }
)


async def create_phone_number(
    db: AsyncSession,
    *,
    phone_e164: str,
    phone_number_type: str,
    nickname: str | None = None,
    area_code: int | None = None,
    inbound_webhook_url: str | None = None,
    inbound_sms_webhook_url: str | None = None,
    allowed_inbound_country_list: list[str] | None = None,
    allowed_outbound_country_list: list[str] | None = None,
    fallback_number: str | None = None,
    transport: str | None = None,
    termination_uri: str | None = None,
    sip_auth_username: str | None = None,
    sip_auth_password: str | None = None,
    inbound_agents: list[dict[str, Any]] | None = None,
    outbound_agents: list[dict[str, Any]] | None = None,
    inbound_sms_agents: list[dict[str, Any]] | None = None,
    outbound_sms_agents: list[dict[str, Any]] | None = None,
) -> PhoneNumber:
    pn = PhoneNumber(
        phone_e164=phone_e164,
        phone_number_type=phone_number_type,
        nickname=nickname,
        area_code=area_code,
        inbound_webhook_url=inbound_webhook_url,
        inbound_sms_webhook_url=inbound_sms_webhook_url,
        allowed_inbound_country_list=allowed_inbound_country_list,
        allowed_outbound_country_list=allowed_outbound_country_list,
        fallback_number=fallback_number,
        transport=transport,
        termination_uri=termination_uri,
        sip_auth_username=sip_auth_username,
        sip_auth_password=sip_auth_password,
        inbound_agents=inbound_agents,
        outbound_agents=outbound_agents,
        inbound_sms_agents=inbound_sms_agents,
        outbound_sms_agents=outbound_sms_agents,
    )
    db.add(pn)
    await db.flush()
    await db.refresh(pn)
    return pn


async def get_by_e164(db: AsyncSession, phone_e164: str) -> PhoneNumber | None:
    result = await db.execute(select(PhoneNumber).where(PhoneNumber.phone_e164 == phone_e164))
    return result.scalar_one_or_none()


async def update_by_e164(
    db: AsyncSession, phone_e164: str, fields: dict[str, Any]
) -> PhoneNumber | None:
    pn = await get_by_e164(db, phone_e164)
    if pn is None:
        return None
    for key, value in fields.items():
        if key in _UPDATABLE_FIELDS:
            setattr(pn, key, value)
        else:
            raise ValueError(f"unexpected phone_number field: {key!r}")
    await db.flush()
    await db.refresh(pn)
    return pn


async def delete_by_e164(db: AsyncSession, phone_e164: str) -> bool:
    result = cast(
        "CursorResult[Any]",
        await db.execute(delete(PhoneNumber).where(PhoneNumber.phone_e164 == phone_e164)),
    )
    return result.rowcount > 0


async def list_phone_numbers(
    db: AsyncSession, *, limit: int, descending: bool, after_id: uuid.UUID | None
) -> list[PhoneNumber]:
    """Keyset-paginate the org's numbers over (created_at, id). RLS scopes to the caller's org."""
    stmt = select(PhoneNumber)
    if after_id is not None:
        cursor = await db.get(PhoneNumber, after_id)
        if cursor is not None:
            if descending:
                stmt = stmt.where(
                    or_(
                        PhoneNumber.created_at < cursor.created_at,
                        and_(
                            PhoneNumber.created_at == cursor.created_at,
                            PhoneNumber.id < cursor.id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        PhoneNumber.created_at > cursor.created_at,
                        and_(
                            PhoneNumber.created_at == cursor.created_at,
                            PhoneNumber.id > cursor.id,
                        ),
                    )
                )
    if descending:
        stmt = stmt.order_by(PhoneNumber.created_at.desc(), PhoneNumber.id.desc())
    else:
        stmt = stmt.order_by(PhoneNumber.created_at.asc(), PhoneNumber.id.asc())
    stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())
```

- [ ] **Step 4: Run the test — expect PASS.** `cd apps/api && uv run pytest -n0 tests/test_phone_numbers_repo.py -v`
- [ ] **Step 5: Lint + typecheck.** `cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy`
- [ ] **Step 6: Commit.**

```bash
git add apps/api/src/usan_api/repositories/phone_numbers.py apps/api/tests/test_phone_numbers_repo.py
git commit -m "feat(api): add phone_numbers RLS-scoped repository (CRUD + keyset list)"
```

---

### Task 3: Compat schemas + AgentWeight + serializer + cursor codec

**Files:**
- Create: `apps/api/src/usan_api/compat/schemas/phone_numbers.py`
- Modify: `apps/api/src/usan_api/compat/ids.py` (add the opaque phone cursor codec)
- Test: `apps/api/tests/compat/test_phone_number_schemas.py`

**Interfaces:**
- Consumes: `PhoneNumber` (Task 1), `ssrf_guard.validate_webhook_url`.
- Produces:
  - `AgentWeight`, `SipOutboundTrunkConfig`, `PhoneNumberResponse`, `ImportPhoneNumberRequest`, `UpdatePhoneNumberRequest` (Pydantic models).
  - `serialize_phone_number(pn: PhoneNumber) -> PhoneNumberResponse`.
  - `ids.encode_phone_number_cursor(pid: uuid.UUID) -> str`, `ids.decode_phone_number_cursor(token: str) -> uuid.UUID` (raises `CompatError(422)` on malformed).

- [ ] **Step 1: Write the failing test** `apps/api/tests/compat/test_phone_number_schemas.py`:

```python
"""Phone-number compat schema unit tests: StrictBool, weight bounds, SSRF URL, serializer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.phone_numbers import (
    AgentWeight,
    ImportPhoneNumberRequest,
    serialize_phone_number,
)
from usan_api.db.models import PhoneNumber


def test_ignore_e164_validation_rejects_string_literal() -> None:
    # StrictBool: the JSON string "true" is NOT coerced — oracle requires a bool literal.
    with pytest.raises(ValidationError):
        ImportPhoneNumberRequest(
            phone_number="+15550000001", termination_uri="x", ignore_e164_validation="true"
        )


def test_agent_weight_bounds() -> None:
    AgentWeight(agent_id="agent_" + uuid.uuid4().hex, weight=1.0)  # ok
    with pytest.raises(ValidationError):
        AgentWeight(agent_id="agent_x", weight=0)  # gt=0
    with pytest.raises(ValidationError):
        AgentWeight(agent_id="", weight=0.5)  # min_length=1


def test_inbound_webhook_url_ssrf_rejected() -> None:
    with pytest.raises(ValidationError):
        ImportPhoneNumberRequest(
            phone_number="+15550000001",
            termination_uri="x",
            inbound_webhook_url="http://169.254.169.254/latest",  # not https + IP literal
        )


def test_serializer_omits_password_and_builds_trunk_config() -> None:
    pn = PhoneNumber(
        id=uuid.uuid4(),
        phone_e164="+15550000001",
        phone_number_type="custom",
        termination_uri="sip.example.com",
        sip_auth_username="user",
        sip_auth_password="secret",
        transport="TCP",
        updated_at=datetime.now(UTC),
    )
    out = serialize_phone_number(pn).model_dump(exclude_none=True)
    assert out["phone_number"] == "+15550000001"
    assert out["phone_number_type"] == "custom"
    assert isinstance(out["last_modification_timestamp"], int)
    assert out["sip_outbound_trunk_config"] == {
        "termination_uri": "sip.example.com",
        "auth_username": "user",
        "transport": "TCP",
    }
    # password never surfaces, anywhere
    assert "auth_password" not in out["sip_outbound_trunk_config"]
    assert "secret" not in str(out)
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError`).

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_phone_number_schemas.py -v`

- [ ] **Step 3: Implement the schemas** `apps/api/src/usan_api/compat/schemas/phone_numbers.py`:

```python
"""RetellAI-compat phone-number request/response schemas + serializer.

Oracle field-name traps preserved: import uses sip_trunk_auth_*, update uses auth_*; the
response NEVER carries auth_password. sms-agent fields + inbound_sms_webhook_url are
update/response only. nickname is plain-optional on import (oracle: not nullable) and
nullable on update. ignore_e164_validation is a StrictBool (string "true"/"false" invalid).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, field_validator

from usan_api.db.models import PhoneNumber
from usan_api.ssrf_guard import validate_webhook_url

_TRANSPORTS = frozenset({"TLS", "TCP", "UDP"})


def _check_webhook(v: str) -> str:
    validate_webhook_url(v)  # raises ValueError -> 422 via the global handler
    return v


def _check_transport(v: str) -> str:
    if v.upper() not in _TRANSPORTS:
        raise ValueError("transport must be one of TLS, TCP, UDP")
    return v


# Reusable, shared across models. AfterValidator runs only on the str branch of `… | None`,
# so the helpers never see None (no None-guard needed) and the wiring is unambiguous in v2.
WebhookUrl = Annotated[str, AfterValidator(_check_webhook)]
Transport = Annotated[str, AfterValidator(_check_transport)]


class AgentWeight(BaseModel):
    model_config = ConfigDict(extra="ignore")
    agent_id: str = Field(min_length=1)
    weight: float = Field(gt=0, le=1)
    agent_version: int | str | None = None

    @field_validator("agent_version")
    @classmethod
    def _nonneg(cls, v: int | str | None) -> int | str | None:
        if isinstance(v, int) and v < 0:
            raise ValueError("agent_version must be >= 0")
        return v


class SipOutboundTrunkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    termination_uri: str | None = None
    auth_username: str | None = None
    transport: str | None = None
    # NO auth_password — write-only, never echoed.


class PhoneNumberResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone_number: str
    phone_number_type: str
    last_modification_timestamp: int
    phone_number_pretty: str | None = None
    area_code: int | None = None
    nickname: str | None = None
    inbound_webhook_url: str | None = None
    inbound_sms_webhook_url: str | None = None
    allowed_inbound_country_list: list[str] | None = None
    allowed_outbound_country_list: list[str] | None = None
    inbound_agents: list[AgentWeight] | None = None
    outbound_agents: list[AgentWeight] | None = None
    inbound_sms_agents: list[AgentWeight] | None = None
    outbound_sms_agents: list[AgentWeight] | None = None
    sip_outbound_trunk_config: SipOutboundTrunkConfig | None = None
    fallback_number: str | None = None


class ImportPhoneNumberRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    phone_number: str = Field(min_length=1)
    termination_uri: str
    ignore_e164_validation: StrictBool = True
    sip_trunk_auth_username: str | None = None
    sip_trunk_auth_password: str | None = None
    inbound_agents: list[AgentWeight] | None = None
    outbound_agents: list[AgentWeight] | None = None
    nickname: str | None = None
    inbound_webhook_url: WebhookUrl | None = None
    allowed_inbound_country_list: list[str] | None = None
    allowed_outbound_country_list: list[str] | None = None
    transport: Transport | None = None


class UpdatePhoneNumberRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    inbound_agents: list[AgentWeight] | None = None
    outbound_agents: list[AgentWeight] | None = None
    inbound_sms_agents: list[AgentWeight] | None = None
    outbound_sms_agents: list[AgentWeight] | None = None
    nickname: str | None = None
    inbound_webhook_url: WebhookUrl | None = None
    inbound_sms_webhook_url: WebhookUrl | None = None
    allowed_inbound_country_list: list[str] | None = None
    allowed_outbound_country_list: list[str] | None = None
    termination_uri: str | None = None
    auth_username: str | None = None
    auth_password: str | None = None
    transport: Transport | None = None
    fallback_number: str | None = None


def _agents(raw: list[dict[str, Any]] | None) -> list[AgentWeight] | None:
    return [AgentWeight(**a) for a in raw] if raw else None


def serialize_phone_number(pn: PhoneNumber) -> PhoneNumberResponse:
    trunk = None
    if pn.termination_uri or pn.sip_auth_username or pn.transport:
        trunk = SipOutboundTrunkConfig(
            termination_uri=pn.termination_uri,
            auth_username=pn.sip_auth_username,
            transport=pn.transport or "TCP",
        )
    return PhoneNumberResponse(
        phone_number=pn.phone_e164,
        phone_number_type=pn.phone_number_type,
        last_modification_timestamp=int(pn.updated_at.timestamp() * 1000),
        # phone_number_pretty + area_code reserved (always null in Phase 2; omitted by exclude_none)
        nickname=pn.nickname,
        inbound_webhook_url=pn.inbound_webhook_url,
        inbound_sms_webhook_url=pn.inbound_sms_webhook_url,
        allowed_inbound_country_list=pn.allowed_inbound_country_list,
        allowed_outbound_country_list=pn.allowed_outbound_country_list,
        inbound_agents=_agents(pn.inbound_agents),
        outbound_agents=_agents(pn.outbound_agents),
        inbound_sms_agents=_agents(pn.inbound_sms_agents),
        outbound_sms_agents=_agents(pn.outbound_sms_agents),
        sip_outbound_trunk_config=trunk,
        fallback_number=pn.fallback_number,
    )
```

- [ ] **Step 4: Add the cursor codec** to `apps/api/src/usan_api/compat/ids.py` (append after `decode_batch_id`):

```python
def encode_phone_number_cursor(pid: uuid.UUID) -> str:
    # Opaque cursor over the INTERNAL row id (bare hex) — never the E.164 (PHI).
    return pid.hex


def decode_phone_number_cursor(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix="", kind="pagination_key")
```

- [ ] **Step 5: Run the schema test — expect PASS.** `cd apps/api && uv run pytest -n0 tests/compat/test_phone_number_schemas.py -v`
- [ ] **Step 6: Lint + typecheck.** `cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy`
- [ ] **Step 7: Commit.**

```bash
git add apps/api/src/usan_api/compat/schemas/phone_numbers.py apps/api/src/usan_api/compat/ids.py apps/api/tests/compat/test_phone_number_schemas.py
git commit -m "feat(api): add compat phone-number schemas, serializer, and cursor codec"
```

---

### Task 4: Phone-number router — `import` / `get` / `delete` (+ register, − 3 stubs)

**Files:**
- Create: `apps/api/src/usan_api/compat/routers/phone_numbers.py`
- Modify: `apps/api/src/usan_api/compat/app.py` (import + `include_router`)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove the import/get/delete `_UNSUPPORTED` entries)
- Test: `apps/api/tests/compat/test_phone_numbers_frozen.py`

**Interfaces:**
- Consumes: Task 2 repo, Task 3 schemas/serializer, `ids.decode_agent_id`, `profiles_repo.get_profile`, `ProfileStatus`.
- Produces: `router` with `POST /import-phone-number`, `GET /get-phone-number/{phone_number}`, `DELETE /delete-phone-number/{phone_number}`; module-local `_audit`, `_E164_RE`, `_resolve_binding_agents`, `_binding_dicts`, `_UPDATE_COLUMN_MAP`. Task 5 extends this same file.

- [ ] **Step 1: Write the failing test** `apps/api/tests/compat/test_phone_numbers_frozen.py`:

```python
"""Frozen conformance + behavior for the compat phone-number surface (Phase 2)."""

from __future__ import annotations

import uuid

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

pytestmark = pytest.mark.frozen


def _published_agent(compat_client, compat_headers) -> str:
    from tests.conftest import _create_and_publish_seed_agent

    return _create_and_publish_seed_agent(compat_client, compat_headers)


def test_import_get_delete_lifecycle(compat_client, compat_headers) -> None:
    agent_id = _published_agent(compat_client, compat_headers)
    r = compat_client.post(
        "/import-phone-number",
        json={
            "phone_number": "+15550000001",
            "termination_uri": "sip.example.com",
            "sip_trunk_auth_username": "u",
            "sip_trunk_auth_password": "p",
            "outbound_agents": [{"agent_id": agent_id, "weight": 1}],
            "nickname": "main line",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["phone_number"] == "+15550000001"
    assert body["phone_number_type"] == "custom"
    assert "auth_password" not in body.get("sip_outbound_trunk_config", {})
    assert "p" not in r.text  # password never echoed
    assert_conforms(body, "PhoneNumberResponse")
    assert_sdk_roundtrip(body, "retell.types:PhoneNumberResponse")

    g = compat_client.get("/get-phone-number/+15550000001", headers=compat_headers)
    assert g.status_code == 200
    assert g.json()["outbound_agents"] == [{"agent_id": agent_id, "weight": 1.0}]

    d = compat_client.delete("/delete-phone-number/+15550000001", headers=compat_headers)
    assert d.status_code == 204
    assert d.content == b""
    assert (
        compat_client.get("/get-phone-number/+15550000001", headers=compat_headers).status_code
        == 404
    )


def test_duplicate_import_is_400(compat_client, compat_headers) -> None:
    payload = {"phone_number": "+15550000009", "termination_uri": "sip.example.com"}
    assert (
        compat_client.post("/import-phone-number", json=payload, headers=compat_headers).status_code
        == 201
    )
    dup = compat_client.post("/import-phone-number", json=payload, headers=compat_headers)
    assert dup.status_code == 400


def test_unknown_binding_agent_is_422(compat_client, compat_headers) -> None:
    r = compat_client.post(
        "/import-phone-number",
        json={
            "phone_number": "+15550000010",
            "termination_uri": "sip.example.com",
            "outbound_agents": [{"agent_id": "agent_" + uuid.uuid4().hex, "weight": 1}],
        },
        headers=compat_headers,
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Run it — expect FAIL** (404/405: routes not registered yet).

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_phone_numbers_frozen.py -v`

- [ ] **Step 3: Implement the router** `apps/api/src/usan_api/compat/routers/phone_numbers.py`:

```python
"""RetellAI-compat phone-number surface (Phase 2): import/get/update/list/delete.

Agent bindings (inbound/outbound[/_sms]_agents) are PERSISTED and echoed but NOT yet honored
at call-routing time — the runtime call-plane is single-org and outbound dial uses the global
caller-id. See docs/deployment/phone-numbers-bindings-deferred.md. create-phone-number stays a
documented 501 (Telnyx DID purchase unavailable).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.phone_numbers import (
    ImportPhoneNumberRequest,
    PhoneNumberResponse,
    UpdatePhoneNumberRequest,
    serialize_phone_number,
)
from usan_api.db.base import ProfileStatus
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import phone_numbers as phones_repo

router = APIRouter(tags=["compat-phone-numbers"])

# E.164: leading +, country digit 1-9, up to 14 more digits.
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

# update-request field -> ORM column (the auth_* vs sip_auth_* divergence).
_UPDATE_COLUMN_MAP = {"auth_username": "sip_auth_username", "auth_password": "sip_auth_password"}


def _audit(request: Request, op: str) -> None:
    # PHI-free: org + op only. NEVER the E.164 (it is the path param, masked in access logs)
    # and NEVER any sip auth secret.
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op).info("compat phone op={op}")


async def _resolve_binding_agents(db: AsyncSession, *lists: list[Any] | None) -> None:
    """Validate every AgentWeight.agent_id resolves to a non-archived org AgentProfile -> 422.

    decode_agent_id already raises CompatError(422) on a malformed id; an unknown/archived
    (well-formed) id is treated as a body-validation failure (422), not a 404 path-not-found.
    """
    for agents in lists:
        if not agents:
            continue
        for aw in agents:
            profile_id = ids.decode_agent_id(aw.agent_id)  # CompatError(422) on malformed
            profile = await profiles_repo.get_profile(db, profile_id)
            if profile is None or profile.status == ProfileStatus.ARCHIVED:
                raise CompatError(422, f"invalid request: unknown agent_id {aw.agent_id}")


def _binding_dicts(agents: list[Any] | None) -> list[dict[str, Any]] | None:
    return [aw.model_dump(exclude_none=True) for aw in agents] if agents else None


@router.post(
    "/import-phone-number",
    status_code=status.HTTP_201_CREATED,
    response_model=PhoneNumberResponse,
    response_model_exclude_none=True,
)
async def import_phone_number(
    body: ImportPhoneNumberRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> PhoneNumberResponse:
    if not body.ignore_e164_validation and not _E164_RE.match(body.phone_number):
        raise CompatError(400, "invalid E.164 phone number")
    await _resolve_binding_agents(db, body.inbound_agents, body.outbound_agents)
    if await phones_repo.get_by_e164(db, body.phone_number) is not None:
        raise CompatError(400, "phone number already imported")
    pn = await phones_repo.create_phone_number(
        db,
        phone_e164=body.phone_number,
        phone_number_type="custom",  # BYO SIP number
        nickname=body.nickname,
        inbound_webhook_url=body.inbound_webhook_url,
        allowed_inbound_country_list=body.allowed_inbound_country_list,
        allowed_outbound_country_list=body.allowed_outbound_country_list,
        transport=body.transport,
        termination_uri=body.termination_uri,
        sip_auth_username=body.sip_trunk_auth_username,
        sip_auth_password=body.sip_trunk_auth_password,
        inbound_agents=_binding_dicts(body.inbound_agents),
        outbound_agents=_binding_dicts(body.outbound_agents),
    )
    _audit(request, "import-phone-number")
    return serialize_phone_number(pn)


@router.get(
    "/get-phone-number/{phone_number}",
    response_model=PhoneNumberResponse,
    response_model_exclude_none=True,
)
async def get_phone_number(
    phone_number: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> PhoneNumberResponse:
    pn = await phones_repo.get_by_e164(db, phone_number)
    if pn is None:
        raise CompatError(404, "phone number not found")
    _audit(request, "get-phone-number")
    return serialize_phone_number(pn)


@router.delete("/delete-phone-number/{phone_number}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_phone_number(
    phone_number: str,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> Response:
    if not await phones_repo.delete_by_e164(db, phone_number):
        raise CompatError(404, "phone number not found")
    _audit(request, "delete-phone-number")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Register the router** in `apps/api/src/usan_api/compat/app.py` — add the import alongside the others:

```python
from usan_api.compat.routers import phone_numbers as compat_phone_numbers
```

and, inside `build_compat_app`, add before `app.include_router(compat_unsupported.router)`:

```python
    app.include_router(compat_phone_numbers.router)
```

- [ ] **Step 5: Remove the 3 served stubs** from `apps/api/src/usan_api/compat/routers/unsupported.py` `_UNSUPPORTED` — delete exactly these three lines (KEEP `("POST", "/create-phone-number")` and, for now, the list + update entries which Task 5 removes):

```python
    ("POST", "/import-phone-number"),
    ("GET", "/get-phone-number/{phone_number}"),
    ("DELETE", "/delete-phone-number/{phone_number}"),
```

- [ ] **Step 6: Run the phone-number test + surface-coverage — expect PASS.**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_phone_numbers_frozen.py tests/compat/test_surface_coverage.py -v`
Expected: PASS. (Surface coverage stays green: the 3 paths are now served real routes and removed from `_UNSUPPORTED`; `KNOWN_GAPS` is still empty.)

- [ ] **Step 7: Lint + typecheck.** `cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy`
- [ ] **Step 8: Commit.**

```bash
git add apps/api/src/usan_api/compat/routers/phone_numbers.py apps/api/src/usan_api/compat/app.py apps/api/src/usan_api/compat/routers/unsupported.py apps/api/tests/compat/test_phone_numbers_frozen.py
git commit -m "feat(api): serve compat import/get/delete-phone-number"
```

---

### Task 5: Phone-number router — `update` / `list` (+ − 2 stubs)

**Files:**
- Modify: `apps/api/src/usan_api/compat/routers/phone_numbers.py` (add `update` + `list` handlers)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove the update + list `_UNSUPPORTED` entries)
- Test: `apps/api/tests/compat/test_phone_numbers_frozen.py` (add cases)

**Interfaces:**
- Consumes: Task 4 router internals (`_audit`, `_resolve_binding_agents`, `_UPDATE_COLUMN_MAP`), repo `update_by_e164`/`list_phone_numbers`, `ids.encode/decode_phone_number_cursor`.

- [ ] **Step 1: Add the failing tests** to `apps/api/tests/compat/test_phone_numbers_frozen.py`:

```python
def test_update_merge_and_traps(compat_client, compat_headers) -> None:
    compat_client.post(
        "/import-phone-number",
        json={"phone_number": "+15550000020", "termination_uri": "sip.example.com", "nickname": "x"},
        headers=compat_headers,
    )
    # update uses auth_* (NOT sip_trunk_auth_*); nickname nullable here; sms fields allowed.
    u = compat_client.patch(
        "/update-phone-number/+15550000020",
        json={
            "nickname": None,
            "auth_username": "u2",
            "auth_password": "p2",
            "inbound_sms_webhook_url": "https://sms.example.com:443/hook",
        },
        headers=compat_headers,
    )
    assert u.status_code == 200, u.text
    body = u.json()
    assert "nickname" not in body  # cleared -> omitted by exclude_none
    assert body["sip_outbound_trunk_config"]["auth_username"] == "u2"
    assert "auth_password" not in body["sip_outbound_trunk_config"]
    assert "p2" not in u.text
    assert_conforms(body, "PhoneNumberResponse")

    assert (
        compat_client.patch(
            "/update-phone-number/+19999999999", json={"nickname": "z"}, headers=compat_headers
        ).status_code
        == 404
    )


def test_list_is_paginated_envelope(compat_client, compat_headers) -> None:
    for n in range(3):
        compat_client.post(
            "/import-phone-number",
            json={"phone_number": f"+1555000003{n}", "termination_uri": "sip.example.com"},
            headers=compat_headers,
        )
    r = compat_client.get("/v2/list-phone-numbers?limit=2", headers=compat_headers)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["items"], list) and len(body["items"]) == 2
    assert body["has_more"] is True
    assert "pagination_key" in body
    for item in body["items"]:
        assert_conforms(item, "PhoneNumberResponse")
    assert_sdk_roundtrip(body, "retell.types:PhoneNumberListResponse")
```

- [ ] **Step 2: Run them — expect FAIL** (405 / route missing).

Run: `cd apps/api && uv run pytest -n0 "tests/compat/test_phone_numbers_frozen.py::test_update_merge_and_traps" "tests/compat/test_phone_numbers_frozen.py::test_list_is_paginated_envelope" -v`

- [ ] **Step 3: Add the handlers** to the END of `apps/api/src/usan_api/compat/routers/phone_numbers.py`:

```python
@router.patch(
    "/update-phone-number/{phone_number}",
    response_model=PhoneNumberResponse,
    response_model_exclude_none=True,
)
async def update_phone_number(
    phone_number: str,
    body: UpdatePhoneNumberRequest,
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
) -> PhoneNumberResponse:
    await _resolve_binding_agents(
        db,
        body.inbound_agents,
        body.outbound_agents,
        body.inbound_sms_agents,
        body.outbound_sms_agents,
    )
    # exclude_unset: only the fields the client sent (an explicit null clears that column).
    provided = body.model_dump(exclude_unset=True)
    fields: dict[str, Any] = {}
    for key, value in provided.items():
        if key in (
            "inbound_agents",
            "outbound_agents",
            "inbound_sms_agents",
            "outbound_sms_agents",
        ):
            fields[key] = value  # already list[dict] | None from model_dump
        else:
            fields[_UPDATE_COLUMN_MAP.get(key, key)] = value
    pn = await phones_repo.update_by_e164(db, phone_number, fields)
    if pn is None:
        raise CompatError(404, "phone number not found")
    _audit(request, "update-phone-number")
    return serialize_phone_number(pn)


@router.get("/v2/list-phone-numbers")
async def list_phone_numbers(
    request: Request,
    sort_order: str = Query(default="descending"),
    limit: int = Query(default=50, ge=1, le=1000),
    pagination_key: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
) -> dict[str, Any]:
    after_id = None
    if pagination_key:
        import contextlib

        with contextlib.suppress(CompatError):  # unparseable cursor -> first page (lenient)
            after_id = ids.decode_phone_number_cursor(pagination_key)
    rows = await phones_repo.list_phone_numbers(
        db, limit=limit, descending=(sort_order != "ascending"), after_id=after_id
    )
    _audit(request, "list-phone-numbers")
    items = [serialize_phone_number(p).model_dump(exclude_none=True) for p in rows]
    out: dict[str, Any] = {"items": items, "has_more": len(rows) == limit}
    if rows and len(rows) == limit:
        out["pagination_key"] = ids.encode_phone_number_cursor(rows[-1].id)
    return out
```

- [ ] **Step 4: Remove the 2 served stubs** from `apps/api/src/usan_api/compat/routers/unsupported.py` `_UNSUPPORTED` — delete exactly:

```python
    ("GET", "/v2/list-phone-numbers"),
    ("PATCH", "/update-phone-number/{phone_number}"),
```

(Now only `("POST", "/create-phone-number")` remains for phone numbers.)

- [ ] **Step 5: Run the phone tests + surface-coverage — expect PASS.**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_phone_numbers_frozen.py tests/compat/test_surface_coverage.py -v`

- [ ] **Step 6: Lint + typecheck.** `cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy`
- [ ] **Step 7: Commit.**

```bash
git add apps/api/src/usan_api/compat/routers/phone_numbers.py apps/api/src/usan_api/compat/routers/unsupported.py apps/api/tests/compat/test_phone_numbers_frozen.py
git commit -m "feat(api): serve compat update/list-phone-numbers"
```

---

### Task 6: Export-requests empty-list stub

**Files:**
- Create: `apps/api/src/usan_api/compat/routers/export_requests.py`
- Modify: `apps/api/src/usan_api/compat/app.py` (import + `include_router`)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (remove the export entry)
- Test: `apps/api/tests/compat/test_export_requests_frozen.py`

**Interfaces:**
- Produces: `router` with `GET /v2/list-export-requests` returning `{"items": [], "has_more": False}`.

- [ ] **Step 1: Write the failing test** `apps/api/tests/compat/test_export_requests_frozen.py`:

```python
"""Frozen: GET /v2/list-export-requests is a conformant empty-list stub (Phase 2)."""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_sdk_roundtrip

pytestmark = pytest.mark.frozen


def test_list_export_requests_is_empty(compat_client, compat_headers) -> None:
    r = compat_client.get(
        "/v2/list-export-requests?limit=50&sort_order=descending&pagination_key=anything",
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"items": [], "has_more": False}
    assert "pagination_key" not in body
    # SDK round-trip (ExportRequestListResponse is an SDK type; there is no oracle component).
    assert_sdk_roundtrip(body, "retell.types:ExportRequestListResponse")
```

- [ ] **Step 2: Run it — expect FAIL** (501 stub still active).

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_export_requests_frozen.py -v`

- [ ] **Step 3: Implement the router** `apps/api/src/usan_api/compat/routers/export_requests.py`:

```python
"""RetellAI-compat analytics/export surface (Phase 2): list-export-requests only.

The oracle exposes NO create/get-by-id export op — a parity client cannot enqueue an export
through the API, so a fresh org genuinely has none. This is a shape-conformant empty-list stub
(no table/poller/GCS). A real async export job is a documented follow-up.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from loguru import logger

router = APIRouter(tags=["compat-export-requests"])


def _audit(request: Request, op: str) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op).info("compat export op={op}")


@router.get("/v2/list-export-requests")
async def list_export_requests(
    request: Request,
    sort_order: str = Query(default="descending"),
    limit: int = Query(default=50, ge=1, le=1000),
    pagination_key: str | None = Query(default=None),
) -> dict[str, Any]:
    # Params accepted + validated, then ignored: there are no export rows to page.
    _audit(request, "list-export-requests")
    return {"items": [], "has_more": False}
```

- [ ] **Step 4: Register the router** in `apps/api/src/usan_api/compat/app.py` — add the import:

```python
from usan_api.compat.routers import export_requests as compat_export_requests
```

and, inside `build_compat_app`, before `app.include_router(compat_unsupported.router)`:

```python
    app.include_router(compat_export_requests.router)
```

- [ ] **Step 5: Remove the export stub** from `apps/api/src/usan_api/compat/routers/unsupported.py` `_UNSUPPORTED` — delete:

```python
    ("GET", "/v2/list-export-requests"),
```

- [ ] **Step 6: Run the export test + surface-coverage — expect PASS.**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_export_requests_frozen.py tests/compat/test_surface_coverage.py -v`

- [ ] **Step 7: Lint + typecheck.** `cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy`
- [ ] **Step 8: Commit.**

```bash
git add apps/api/src/usan_api/compat/routers/export_requests.py apps/api/src/usan_api/compat/app.py apps/api/src/usan_api/compat/routers/unsupported.py apps/api/tests/compat/test_export_requests_frozen.py
git commit -m "feat(api): serve compat list-export-requests as conformant empty-list stub"
```

---

### Task 7: Mask the raw E.164 in access logs

**Files:**
- Modify: `apps/api/src/usan_api/logging_config.py` (`_InterceptHandler.emit` + a pure `_mask_phi_path` helper)
- Test: `apps/api/tests/test_logging_phi_mask.py`

**Interfaces:**
- Produces: `logging_config._mask_phi_path(message: str) -> str` (pure, testable). The E.164 path segment of `*-phone-number/<E164>` requests is replaced with `[redacted]` before the access line reaches the sink. The oracle forces `{phone_number}` (literal E.164) as the path param, so it cannot be opaque-encoded like `call_id`.

- [ ] **Step 1: Write the failing test** `apps/api/tests/test_logging_phi_mask.py`:

```python
"""The raw E.164 phone-number path segment is redacted from uvicorn.access log lines."""

from __future__ import annotations

from usan_api.logging_config import _mask_phi_path


def test_masks_phone_number_path_segment() -> None:
    line = '127.0.0.1:54321 - "GET /get-phone-number/+19495551234 HTTP/1.1" 200'
    out = _mask_phi_path(line)
    assert "+19495551234" not in out
    assert "/get-phone-number/[redacted]" in out


def test_masks_update_and_delete_too() -> None:
    assert "+15550001111" not in _mask_phi_path("PATCH /update-phone-number/+15550001111 ...")
    assert "+15550002222" not in _mask_phi_path("DELETE /delete-phone-number/+15550002222 ...")


def test_leaves_other_paths_untouched() -> None:
    line = "GET /v2/list-phone-numbers?limit=50 HTTP/1.1"
    assert _mask_phi_path(line) == line
```

- [ ] **Step 2: Run it — expect FAIL** (`ImportError: cannot import name '_mask_phi_path'`).

Run: `cd apps/api && uv run pytest -n0 tests/test_logging_phi_mask.py -v`

- [ ] **Step 3: Implement masking** in `apps/api/src/usan_api/logging_config.py` — add `import re` to the imports if absent, add the helper near the top (after the imports), and call it in `_InterceptHandler.emit`:

```python
# Redact the raw E.164 segment of get/update/delete-phone-number paths from access logs:
# the oracle forces {phone_number} (literal E.164) as the path param, so it cannot be
# opaque-encoded like call_id. The audit log already binds ids only.
_PHONE_PATH_RE = re.compile(r"(/(?:get|update|delete)-phone-number/)\+?[0-9]+")


def _mask_phi_path(message: str) -> str:
    return _PHONE_PATH_RE.sub(r"\1[redacted]", message)
```

Then change `_InterceptHandler.emit` to mask the message once and reuse it:

```python
    def emit(self, record: logging.LogRecord) -> None:
        message = _mask_phi_path(record.getMessage())
        if record.name == "uvicorn.access" and "/health" in message:
            return
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk back past the logging machinery so loguru reports the real caller.
        frame, depth = inspect.currentframe(), 0
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, message)
```

- [ ] **Step 4: Run the test — expect PASS.** `cd apps/api && uv run pytest -n0 tests/test_logging_phi_mask.py -v`
- [ ] **Step 5: Lint + typecheck.** `cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy`
- [ ] **Step 6: Commit.**

```bash
git add apps/api/src/usan_api/logging_config.py apps/api/tests/test_logging_phi_mask.py
git commit -m "feat(api): redact raw E.164 phone-number path segment from access logs"
```

---

### Task 8: `create-phone-number` stays-501 frozen test + honoring-deferred doc

**Files:**
- Create: `docs/deployment/phone-numbers-bindings-deferred.md`
- Test: add `test_create_phone_number_still_501` to `apps/api/tests/compat/test_phone_numbers_frozen.py`

**Interfaces:**
- Consumes: the served surface from Tasks 4–6.

- [ ] **Step 1: Write the test** — append to `apps/api/tests/compat/test_phone_numbers_frozen.py`:

```python
def test_create_phone_number_still_501(compat_client, compat_headers) -> None:
    # create requires a Telnyx DID purchase the engine cannot perform — documented 501.
    r = compat_client.post("/create-phone-number", json={"area_code": 415}, headers=compat_headers)
    assert r.status_code == 501
    assert r.json() == {"status": 501, "message": "not_supported: /create-phone-number"}
```

- [ ] **Step 2: Run it — expect PASS already** (the stub is intact from Task 4/5, which kept the create entry).

Run: `cd apps/api && uv run pytest -n0 "tests/compat/test_phone_numbers_frozen.py::test_create_phone_number_still_501" -v`
Expected: PASS. (This is a lock, not a behavior change — if it FAILS, a prior task wrongly removed the create stub.)

- [ ] **Step 3: Write the deferral doc** `docs/deployment/phone-numbers-bindings-deferred.md`:

```markdown
# Phone-number bindings: persisted, not yet honored

The compat phone-number surface (`import`/`get`/`update`/`list`/`delete`) persists agent
bindings (`inbound_agents`, `outbound_agents`, `inbound_sms_agents`, `outbound_sms_agents`)
and echoes them back truthfully, but **does not yet honor them at call-routing time**.

Why: the runtime call-plane is single-org and outbound dial uses a single global caller-id
(`settings.telnyx_caller_id`) with a process-wide, org-blind trunk cache; inbound routing is
runtime-only LiveKit SIP dispatch state. Honoring per-number bindings means rewiring the live
outbound dial path and adding an inbound DID→agent map — a change to live-call routing that is
gated on the multi-org call-plane and a concrete client. No endpoint fakes routing: a bound
number returns 200 and stores the binding, and that is all it claims to do.

`create-phone-number` is a documented 501: it requires purchasing a DID via the Telnyx Numbers
API (no client, no key, real spend) — a separate future phase.

Known surface-wide deviations (not Phase 2's to fix; see the design spec §13): not-found
returns the house `404` (oracle declares `422`), and the error envelope `status` is the int
HTTP code (oracle declares the string `"error"`).
```

- [ ] **Step 4: Run the full phone + export + surface suite — expect PASS.**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_phone_numbers_frozen.py tests/compat/test_export_requests_frozen.py tests/compat/test_surface_coverage.py -v`

- [ ] **Step 5: Lint + typecheck.** `cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy`
- [ ] **Step 6: Commit.**

```bash
git add docs/deployment/phone-numbers-bindings-deferred.md apps/api/tests/compat/test_phone_numbers_frozen.py
git commit -m "docs(api): document deferred phone-number binding honoring + lock create-501"
```

---

## Final verification (after all tasks)

- [ ] Full compat suite + the new tests, serial: `cd apps/api && uv run pytest -n0 tests/compat -v`
- [ ] Full API suite (parallel default): `cd apps/api && uv run pytest` (testcontainer flakes can ERROR under load — re-run any failures in isolation with `-n0` before treating as a regression).
- [ ] `cd apps/api && uv run ruff check . && uv run mypy` (NEVER `mypy .`).
- [ ] `git log --oneline origin/main..HEAD` shows the Task 1–8 commits on `retell-parity-phase2-numbers-exports`.
- [ ] Then `superpowers:finishing-a-development-branch` → push + open a squash-merge PR (no `v*` tag).
