# P2 Multi-Tenant Identity, Membership, RBAC & Act-As — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the admin plane multi-tenant — a person logs in, is scoped to the org(s) they belong to, and USAN staff (super-admins) can act-as any client org with full-write, fully audited.

**Architecture:** A global identity (`admin_users`) + a many-to-many `memberships(email, org_id, role)` join, both non-RLS control-plane tables. The active org rides in the stateless session JWT. `require_admin_session` re-validates membership every request; a new `get_tenant_db` dependency sets `app.current_org` from the principal so P1's RLS scopes all admin reads. The runtime/call plane (service token, webhooks, pollers) stays single-org. Builds on `docs/superpowers/specs/2026-06-16-tenancy-p2-identity-rbac-design.md`.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Postgres RLS (P1), PyJWT (HS256), pytest + testcontainers, React/TS + vitest (admin-ui).

**Migration head:** `0032`. New migrations: `0033` (identity + memberships), `0034` (per-org uniqueness).

**PR boundaries:** Backend Units A–D ship as **PR 1** (a working, fully-tested org-aware API). Admin UI Unit E ships as **PR 2** on top. Both are "P2".

---

## Canonical Contract (single source of truth — every task must match these names/signatures)

**Models** (`apps/api/src/usan_api/db/models.py`):
- `AdminUser` (table `admin_users`, global): `email` (PK), **`is_super_admin: bool`** (`server_default=text("false")`), **`status: str`** (`server_default="active"`), **`last_active_org_id: uuid.UUID | None`** (`ForeignKey("organizations.id")`, nullable), `added_by`, `created_at`. The `role` column is **removed**.
- `Membership` (table `memberships`, global, **no** `TenantScoped`): `email` (`ForeignKey("admin_users.email", ondelete="CASCADE")`, PK part), `organization_id` (`ForeignKey("organizations.id", ondelete="CASCADE")`, PK part), `role: AdminRole` (`SAEnum(AdminRole, name="admin_role", values_callable=_enum_values, create_type=False)`), `added_by: str | None`, `created_at`. Composite PK `(email, organization_id)`.

**Principal** (`apps/api/src/usan_api/auth.py`):
```python
@dataclass(frozen=True)
class AdminPrincipal:
    email: str
    active_org_id: uuid.UUID | None
    role: AdminRole | None       # role in the active org; None when no active org
    is_super_admin: bool
    acting_as: bool              # True when active_org came from act-as, not a membership
```

**Session claims** (`apps/api/src/usan_api/admin_session.py`): `sub`, `active_org` (str UUID or null), `role` (`"admin"`/`"viewer"`/null), `super` (bool), `acting_as` (bool), `typ="admin_session"`, `iat`, `exp`.
- `issue_session(email: str, *, active_org_id: uuid.UUID | None, role: AdminRole | None, is_super_admin: bool, acting_as: bool, settings: Settings) -> str`

**Dependencies** (`apps/api/src/usan_api/auth.py`):
- `require_admin_session(...) -> AdminPrincipal` — keeps `db: AsyncSession = Depends(get_db)` (reads the non-RLS global tables; no circularity).
- `get_tenant_db(principal=Depends(require_admin_session)) -> AsyncIterator[AsyncSession]` — 409 if `active_org_id is None`; opens its own session and `set_tenant_context(session, principal.active_org_id)`.
- `require_admin_role(required: AdminRole)` — unchanged signature; reads `principal.role`.
- `require_super_admin(principal=Depends(require_admin_session)) -> AdminPrincipal` — 403 unless `is_super_admin`.

**Repos:**
- `repositories/memberships.py`: `list_memberships_for_email(db, email) -> list[Membership]`, `get_membership(db, email, org_id) -> Membership | None`, `list_members(db, org_id) -> list[Membership]`, `add_member(db, *, email, org_id, role, added_by) -> Membership`, `set_member_role(db, *, email, org_id, role) -> Membership` (raises `LastOrgAdminError`), `remove_member(db, *, email, org_id) -> bool` (raises `LastOrgAdminError`), `count_org_admins(db, org_id) -> int`.
- `repositories/admin_users.py`: `get_admin_user(db, email)` (identity only), `ensure_identity(db, *, email, is_super_admin=False) -> AdminUser`, `set_last_active_org(db, *, email, org_id)`, `seed_bootstrap(db, emails)` (now sets `is_super_admin=True` + usan ADMIN membership).
- `repositories/organizations.py`: add `create_org(db, *, name, slug) -> Organization`, `list_orgs(db) -> list[Organization]`.

**Endpoints:** `POST /v1/auth/switch-org` (body `{organization_id: uuid}`); `GET /v1/auth/me` (extended); `/v1/admin/members` CRUD (Unit C); `/v1/admin/organizations` GET/POST super-admin only (Unit C).

---

## UNIT A — Schema, migration, models, repos

### Task A1: `Membership` model + `AdminUser` changes

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py:503-523` (AdminUser), add `Membership` after `AdminUser`.
- Test: `apps/api/tests/test_models_membership.py` (create)

- [ ] **Step 1 — Failing test** (`apps/api/tests/test_models_membership.py`):
```python
from usan_api.db.base import AdminRole
from usan_api.db.models import AdminUser, Membership


def test_admin_user_has_identity_fields_and_no_role():
    cols = AdminUser.__table__.c
    assert "is_super_admin" in cols
    assert "status" in cols
    assert "last_active_org_id" in cols
    assert "role" not in cols  # moved to Membership


def test_membership_composite_pk_and_role():
    pk = {c.name for c in Membership.__table__.primary_key.columns}
    assert pk == {"email", "organization_id"}
    assert "role" in Membership.__table__.c
```

- [ ] **Step 2 — Run, expect FAIL** (`Membership` undefined / `role` still present):
`cd apps/api && uv run pytest tests/test_models_membership.py -q`

- [ ] **Step 3 — Edit `AdminUser`** (replace lines 503-523). Remove the `role` column; add the three identity columns:
```python
class AdminUser(Base):
    """Global identity — the person. Per-org role lives in Membership (P2)."""

    __tablename__ = "admin_users"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    is_super_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    last_active_org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id")
    )
    added_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Membership(Base):
    """Many-to-many: which person has which role in which org. Global, non-RLS."""

    __tablename__ = "memberships"

    email: Mapped[str] = mapped_column(
        ForeignKey("admin_users.email", ondelete="CASCADE"), primary_key=True
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[AdminRole] = mapped_column(
        SAEnum(AdminRole, name="admin_role", values_callable=_enum_values, create_type=False),
        nullable=False,
    )
    added_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4 — Run, expect PASS.**
- [ ] **Step 5 — Commit:** `git add -A && git commit -m "feat(api): add Membership model, move role off AdminUser (P2-A)"`

### Task A2: Migration 0033 — identity columns, memberships, data migration

**Files:** Create `apps/api/migrations/versions/0033_memberships.py`. Test: `apps/api/tests/test_memberships_migration.py`.

- [ ] **Step 1 — Failing test** (round-trips through 0033, mirrors the existing `test_webhook_migration` pattern — assert table + columns + data-migrated rows at head):
```python
import sqlalchemy as sa
from sqlalchemy import text


def test_0033_creates_memberships_and_migrates_role(database_url, app_role_password):
    eng = sa.create_engine(database_url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                       "VALUES ('legacy@x.com', false, 'active', 'seed') "
                       "ON CONFLICT (email) DO NOTHING"))
        c.execute(text("INSERT INTO memberships (email, organization_id, role, added_by) "
                       "SELECT 'legacy@x.com', id, CAST('viewer' AS admin_role), 'seed' "
                       "FROM organizations WHERE slug='usan' ON CONFLICT DO NOTHING"))
    with eng.connect() as c:
        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='admin_users'"))}
        assert {"is_super_admin", "status", "last_active_org_id"} <= cols
        assert "role" not in cols
        m = c.execute(text("SELECT role FROM memberships WHERE email='legacy@x.com'")).scalar_one()
        assert m == "viewer"
    eng.dispose()
```
> The session container is already at head, so this asserts the migrated end-state + that the `memberships` table accepts a usan membership. The role-backfill itself is exercised by the round-trip command in Step 4.

- [ ] **Step 2 — Run, expect FAIL** (no `memberships` table yet, before writing the migration / running it fresh).

- [ ] **Step 3 — Write `0033_memberships.py`** (follows the 0031/0032 idiom; data-migrate BEFORE dropping `role`):
```python
"""identity columns + memberships table + role data-migration

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-16
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USAN = "(SELECT id FROM organizations WHERE slug = 'usan')"


def upgrade() -> None:
    # 1. Identity columns on admin_users (global table; no RLS).
    op.add_column("admin_users", sa.Column("is_super_admin", sa.Boolean(),
                  nullable=False, server_default=sa.text("false")))
    op.add_column("admin_users", sa.Column("status", sa.Text(),
                  nullable=False, server_default="active"))
    op.add_column("admin_users", sa.Column("last_active_org_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_admin_users_last_org", "admin_users", "organizations",
                          ["last_active_org_id"], ["id"])

    # 2. memberships table (global, non-RLS).
    op.create_table(
        "memberships",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Enum("admin", "viewer", name="admin_role", create_type=False),
                  nullable=False),
        sa.Column("added_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["email"], ["admin_users.email"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("email", "organization_id"),
    )

    # 3. Data-migrate: each existing operator gets a usan membership at their current role.
    op.execute(f"INSERT INTO memberships (email, organization_id, role, added_by) "
               f"SELECT email, {_USAN}, role, 'migration' FROM admin_users "
               f"ON CONFLICT DO NOTHING")  # noqa: S608 (constant org subquery, no user input)

    # 4. Drop the per-person role column (now per-membership).
    op.drop_column("admin_users", "role")

    # 5. Grant the non-superuser app role access to the new table (mirror 0030's grant form).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memberships TO usan_app")


def downgrade() -> None:
    op.add_column("admin_users", sa.Column("role",
                  sa.Enum("admin", "viewer", name="admin_role", create_type=False),
                  nullable=False, server_default="admin"))
    op.execute(f"UPDATE admin_users a SET role = m.role FROM memberships m "
               f"WHERE m.email = a.email AND m.organization_id = {_USAN}")  # noqa: S608
    op.drop_table("memberships")
    op.drop_constraint("fk_admin_users_last_org", "admin_users", type_="foreignkey")
    op.drop_column("admin_users", "last_active_org_id")
    op.drop_column("admin_users", "status")
    op.drop_column("admin_users", "is_super_admin")
```
> Verify migration 0030's exact `GRANT ... TO usan_app` statement and copy its form for step 5.

- [ ] **Step 4 — Run, expect PASS.** Prove round-trip: `cd apps/api && uv run alembic upgrade head && uv run alembic downgrade 0032 && uv run alembic upgrade head`.
- [ ] **Step 5 — Commit:** `git commit -am "feat(api): migration 0033 — identity columns + memberships + role data-migration (P2-A)"`

### Task A3: memberships repo + admin_users/organizations repo additions

**Files:** Create `apps/api/src/usan_api/repositories/memberships.py`. Modify `repositories/admin_users.py`, `repositories/organizations.py`. Test: `apps/api/tests/test_memberships_repo.py`.

- [ ] **Step 1 — Failing tests** (use a usan_app session + two orgs; assert add/list/role/remove and the per-org last-admin guard):
```python
import pytest
from usan_api.db.base import AdminRole
from usan_api.repositories import memberships as repo
# fixtures `two_orgs`, `app_session` defined in Task D2 conftest additions.

async def test_add_and_list_members(app_session, two_orgs):
    org_a, _ = two_orgs
    await repo.add_member(app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t")
    members = await repo.list_members(app_session, org_a)
    assert [m.email for m in members] == ["a@x.com"]

async def test_remove_last_org_admin_raises(app_session, two_orgs):
    org_a, _ = two_orgs
    await repo.add_member(app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t")
    with pytest.raises(repo.LastOrgAdminError):
        await repo.remove_member(app_session, email="a@x.com", org_id=org_a)
```

- [ ] **Step 2 — Run, expect FAIL** (module missing).

- [ ] **Step 3 — Write `memberships.py`** (`ensure_identity` first so the FK to `admin_users` holds; per-org last-admin guard mirrors `admin_users.count_admins`):
```python
"""Per-org membership data access (P2). memberships + admin_users are GLOBAL
(non-RLS) control-plane tables, so every query here MUST be scoped by
organization_id in app code — that scoping replaces RLS for these tables."""

import uuid
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import AdminRole
from usan_api.db.models import Membership
from usan_api.repositories import admin_users as admin_users_repo


class LastOrgAdminError(Exception):
    """Refuse removing/demoting the last ADMIN of an org (unrecoverable lockout)."""


def _norm(email: str) -> str:
    return email.strip().lower()


async def list_memberships_for_email(db: AsyncSession, email: str) -> list[Membership]:
    res = await db.execute(
        select(Membership).where(Membership.email == _norm(email)).order_by(Membership.created_at)
    )
    return list(res.scalars().all())


async def get_membership(db: AsyncSession, email: str, org_id: uuid.UUID) -> Membership | None:
    return await db.get(Membership, (_norm(email), org_id))


async def list_members(db: AsyncSession, org_id: uuid.UUID) -> list[Membership]:
    res = await db.execute(
        select(Membership).where(Membership.organization_id == org_id).order_by(Membership.email)
    )
    return list(res.scalars().all())


async def count_org_admins(db: AsyncSession, org_id: uuid.UUID) -> int:
    res = await db.execute(
        select(func.count()).select_from(Membership).where(
            Membership.organization_id == org_id, Membership.role == AdminRole.ADMIN
        )
    )
    return int(res.scalar_one())


async def add_member(db: AsyncSession, *, email: str, org_id: uuid.UUID,
                     role: AdminRole, added_by: str | None) -> Membership:
    norm = _norm(email)
    await admin_users_repo.ensure_identity(db, email=norm)  # FK target must exist
    stmt = (
        pg_insert(Membership)
        .values(email=norm, organization_id=org_id, role=role, added_by=added_by)
        .on_conflict_do_update(index_elements=["email", "organization_id"], set_={"role": role})
    )
    await db.execute(stmt)
    await db.flush()
    m = await db.get(Membership, (norm, org_id))
    assert m is not None
    return m


async def set_member_role(db: AsyncSession, *, email: str, org_id: uuid.UUID,
                          role: AdminRole) -> Membership:
    norm = _norm(email)
    m = await db.get(Membership, (norm, org_id))
    if m is None:
        raise KeyError("membership not found")
    if (m.role is AdminRole.ADMIN and role is AdminRole.VIEWER
            and await count_org_admins(db, org_id) <= 1):
        raise LastOrgAdminError("cannot demote the last admin of this org")
    m.role = role
    await db.flush()
    return m


async def remove_member(db: AsyncSession, *, email: str, org_id: uuid.UUID) -> bool:
    norm = _norm(email)
    m = await db.get(Membership, (norm, org_id))
    if m is None:
        return False
    if m.role is AdminRole.ADMIN and await count_org_admins(db, org_id) <= 1:
        raise LastOrgAdminError("cannot remove the last admin of this org")
    await db.execute(
        delete(Membership).where(Membership.email == norm, Membership.organization_id == org_id)
    )
    await db.flush()
    return True
```

- [ ] **Step 4 — Add to `admin_users.py`:** keep `get_admin_user`. Add `ensure_identity` + `set_last_active_org`; rewrite `seed_bootstrap`; remove the role-mutation helpers (`LastAdminError`, `count_admins`, `add_admin_user`, `remove_admin_user`) whose responsibility moved to `memberships.py`:
```python
async def ensure_identity(db: AsyncSession, *, email: str, is_super_admin: bool = False) -> AdminUser:
    norm = _norm(email)
    stmt = (
        pg_insert(AdminUser)
        .values(email=norm, is_super_admin=is_super_admin, added_by="invite")
        .on_conflict_do_nothing(index_elements=["email"])
    )
    await db.execute(stmt)
    await db.flush()
    user = await db.get(AdminUser, norm)
    assert user is not None
    return user


async def set_last_active_org(db: AsyncSession, *, email: str, org_id: uuid.UUID) -> None:
    user = await db.get(AdminUser, _norm(email))
    if user is not None:
        user.last_active_org_id = org_id
        await db.flush()
```
Rewrite `seed_bootstrap(db, emails)` to: for each email, `ensure_identity(db, email=e, is_super_admin=True)`; look up the usan org via `organizations.get_org_by_slug(db, "usan")`; if present, `memberships.add_member(db, email=e, org_id=usan.id, role=AdminRole.ADMIN, added_by="bootstrap")`. Return the count of identities created. (Import `memberships` lazily inside the function to avoid a circular import: `memberships` imports `admin_users`.)

- [ ] **Step 5 — Add to `organizations.py`:**
```python
from usan_api.db.models import Organization  # already imported

async def create_org(db: AsyncSession, *, name: str, slug: str) -> Organization:
    org = Organization(name=name, slug=slug)
    db.add(org)
    await db.flush()
    return org

async def list_orgs(db: AsyncSession) -> list[Organization]:
    res = await db.execute(select(Organization).order_by(Organization.name))
    return list(res.scalars().all())
```
(Add `from sqlalchemy import select` if not present.)

- [ ] **Step 6 — Run repo tests, expect PASS.**
- [ ] **Step 7 — Commit:** `git commit -am "feat(api): memberships repo + identity/org repo helpers (P2-A)"`

---

## UNIT B — Auth, session, switch-org/act-as, get_tenant_db, router swaps

### Task B1: session claims carry active org + role + super + acting_as

**Files:** Modify `apps/api/src/usan_api/admin_session.py:36-55`. Test: `apps/api/tests/test_admin_session.py` (extend or create).

- [ ] **Step 1 — Failing test:**
```python
import uuid
from usan_api.admin_session import issue_session, decode_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

def test_session_round_trips_active_org_and_flags(monkeypatch):
    monkeypatch.setenv("JWT_SIGNING_KEY", "s"*32); monkeypatch.setenv("OPERATOR_API_KEY","o"*32)
    monkeypatch.setenv("DATABASE_URL","postgresql://x/y"); get_settings.cache_clear()
    s = get_settings(); org = uuid.uuid4()
    tok = issue_session("a@x.com", active_org_id=org, role=AdminRole.ADMIN,
                        is_super_admin=True, acting_as=True, settings=s)
    c = decode_session(tok, s)
    assert c["active_org"] == str(org) and c["role"] == "admin"
    assert c["super"] is True and c["acting_as"] is True
```

- [ ] **Step 2 — Run, expect FAIL** (signature mismatch).

- [ ] **Step 3 — Rewrite `issue_session`** (keyword-only new params; `decode_session` is unchanged — it already only requires `exp`,`sub`). Add `import uuid`:
```python
def issue_session(email: str, *, active_org_id: uuid.UUID | None, role: AdminRole | None,
                  is_super_admin: bool, acting_as: bool, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": email,
        "active_org": str(active_org_id) if active_org_id else None,
        "role": role.value if role else None,
        "super": is_super_admin,
        "acting_as": acting_as,
        "typ": "admin_session",
        "iat": now,
        "exp": now + timedelta(seconds=settings.admin_session_ttl_s),
    }
    return jwt.encode(payload, _key(settings), algorithm=_ALG)
```

- [ ] **Step 4 — Run, expect PASS.**
- [ ] **Step 5 — Commit.**

### Task B2: `AdminPrincipal` + `require_admin_session` re-validation + `get_tenant_db` + `require_super_admin`

**Files:** Modify `apps/api/src/usan_api/auth.py`. Test: `apps/api/tests/test_auth_principal.py`.

- [ ] **Step 1 — Failing test** (membership-driven role, instant revocation, act-as guard, 409 on no active org). Build with the multi-org fixtures from Unit D; assert via a tiny app that depends on each dependency. Key assertions:
  - principal for a member resolves `role` from `memberships`, `acting_as=False`;
  - a session whose membership was deleted → 401/403;
  - `acting_as=True` + `super=False` → 401;
  - `get_tenant_db` with `active_org=None` → 409;
  - `require_super_admin` blocks non-super (403).

- [ ] **Step 2 — Run, expect FAIL.**

- [ ] **Step 3 — Rewrite `auth.py`** principal + deps. New imports: `import uuid`, `from collections.abc import AsyncIterator`, `from usan_api.db.session import get_db, get_session_factory`, `from usan_api.tenant_context import set_tenant_context`, `from usan_api.repositories import memberships as memberships_repo`.
```python
@dataclass(frozen=True)
class AdminPrincipal:
    email: str
    active_org_id: uuid.UUID | None
    role: AdminRole | None
    is_super_admin: bool
    acting_as: bool


async def require_admin_session(
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminPrincipal:
    if not session_cookie:
        raise HTTPException(401, "missing session", headers=_COOKIE_AUTH)
    try:
        claims = decode_session(session_cookie, settings)
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "invalid session", headers=_COOKIE_AUTH) from exc
    email = str(claims["sub"]).lower()
    user = await admin_users_repo.get_admin_user(db, email)
    if user is None or user.status != "active":
        raise HTTPException(401, "not authorized", headers=_COOKIE_AUTH)
    active_org_raw = claims.get("active_org")
    active_org = uuid.UUID(active_org_raw) if active_org_raw else None
    acting_as = bool(claims.get("acting_as"))
    role: AdminRole | None = None
    if active_org is not None:
        if acting_as:
            if not user.is_super_admin:
                raise HTTPException(401, "not authorized", headers=_COOKIE_AUTH)
            role = AdminRole.ADMIN
        else:
            m = await memberships_repo.get_membership(db, email, active_org)
            if m is None:
                raise HTTPException(403, "no access to this organization")
            role = m.role
    return AdminPrincipal(email=email, active_org_id=active_org, role=role,
                          is_super_admin=user.is_super_admin, acting_as=acting_as)


async def get_tenant_db(
    principal: AdminPrincipal = Depends(require_admin_session),
) -> AsyncIterator[AsyncSession]:
    if principal.active_org_id is None:
        raise HTTPException(409, "select an organization first")
    async with get_session_factory()() as session:
        try:
            await set_tenant_context(session, principal.active_org_id)
            yield session
        except Exception:
            await session.rollback()
            raise


def require_super_admin(
    principal: AdminPrincipal = Depends(require_admin_session),
) -> AdminPrincipal:
    if not principal.is_super_admin:
        raise HTTPException(403, "super-admin required")
    return principal
```
Keep `require_admin_role` as-is (when `role is None` an ADMIN-required route returns 403 — correct).

- [ ] **Step 4 — Run, expect PASS.**
- [ ] **Step 5 — Commit.**

### Task B3: callback resolves active org; `/me` extended; `switch-org` endpoint

**Files:** Modify `apps/api/src/usan_api/routers/auth.py`, `apps/api/src/usan_api/schemas/auth.py`. Test: `apps/api/tests/test_auth_flow_p2.py`.

- [ ] **Step 1 — Failing tests** (sso_client): 1-membership user lands logged-in with that active org; 0-membership non-super → 403 denied; super-admin with 0 memberships logs in with `active_org=null`; `switch-org` to a membership org succeeds; super-admin `switch-org` to a non-member org sets `acting_as=true` and writes an act-as audit row; non-super `switch-org` to non-member org → 403; `/me` returns `orgs` list + `active_org` + `is_super_admin` + `acting_as`.

- [ ] **Step 2 — Run, expect FAIL.**

- [ ] **Step 3 — Schemas** (`schemas/auth.py`): replace `MeResponse`; add `SwitchOrgRequest` + `OrgSummary`:
```python
import uuid

class OrgSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str | None = None  # the caller's role in this org (None for act-as-only super-admin)

class MeResponse(BaseModel):
    email: str
    is_super_admin: bool
    acting_as: bool
    active_org: OrgSummary | None
    orgs: list[OrgSummary]

class SwitchOrgRequest(BaseModel):
    organization_id: uuid.UUID
```

- [ ] **Step 4 — Callback** (`routers/auth.py`): replace the success block (lines ~98-119) — after verifying the email, load the identity, deny when appropriate, choose the active org, issue the session. Add `from usan_api.repositories import memberships as memberships_repo`:
```python
    user = await admin_users_repo.get_admin_user(db, email)
    if user is None or user.status != "active":
        await admin_audit.record(db, actor_email=email, action="auth.denied",
                                 entity_type="admin_user", entity_id=email)
        await db.commit()
        logger.bind(email=email).warning("SSO login rejected: not allow-listed/active")
        return _fail(status.HTTP_403_FORBIDDEN, "not authorized", settings)

    memberships = await memberships_repo.list_memberships_for_email(db, email)
    if not memberships and not user.is_super_admin:
        await admin_audit.record(db, actor_email=email, action="auth.denied",
                                 entity_type="admin_user", entity_id=email)
        await db.commit()
        return _fail(status.HTTP_403_FORBIDDEN, "not authorized", settings)

    active_org_id = None
    role = None
    if memberships:
        chosen = next((m for m in memberships if m.organization_id == user.last_active_org_id),
                      memberships[0])
        active_org_id, role = chosen.organization_id, chosen.role
        await admin_users_repo.set_last_active_org(db, email=email, org_id=active_org_id)
    await admin_audit.record(db, actor_email=email, action="auth.login",
                             entity_type="admin_user", entity_id=email)
    await db.commit()
    resp = RedirectResponse(settings.admin_post_login_redirect, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(resp, issue_session(
        email, active_org_id=active_org_id, role=role,
        is_super_admin=user.is_super_admin, acting_as=False, settings=settings), settings)
    clear_tx_cookie(resp, settings)
    return resp
```
> The callback uses `get_db` (default-org context); the `auth.login`/`auth.denied` audit rows land in the default org under the connect baseline — acceptable as platform auth events (per-org login trails are a P3 refinement).

- [ ] **Step 5 — `/me`** (`routers/auth.py`): inject `db: AsyncSession = Depends(get_db)`; add `from usan_api.repositories import organizations as organizations_repo`:
```python
@router.get("/me", response_model=MeResponse)
async def me(principal: AdminPrincipal = Depends(require_admin_session),
             db: AsyncSession = Depends(get_db)) -> MeResponse:
    memberships = await memberships_repo.list_memberships_for_email(db, principal.email)
    summaries: list[OrgSummary] = []
    for m in memberships:
        o = await organizations_repo.get_org(db, m.organization_id)
        if o:
            summaries.append(OrgSummary(id=o.id, name=o.name, slug=o.slug, role=m.role.value))
    role_by_org = {m.organization_id: m.role.value for m in memberships}
    active = None
    if principal.active_org_id is not None:
        o = await organizations_repo.get_org(db, principal.active_org_id)
        if o:
            active = OrgSummary(id=o.id, name=o.name, slug=o.slug,
                               role=role_by_org.get(o.id))
    return MeResponse(email=principal.email, is_super_admin=principal.is_super_admin,
                      acting_as=principal.acting_as, active_org=active, orgs=summaries)
```
(Super-admin org browsing for act-as is served by Unit C's `/v1/admin/organizations`, not `/me`.)

- [ ] **Step 6 — `switch-org`** (`routers/auth.py`). Add imports `AdminRole`, `set_tenant_context`, `SwitchOrgRequest`, `OrgSummary`:
```python
@router.post("/switch-org", response_model=MeResponse)
async def switch_org(body: SwitchOrgRequest,
                     principal: AdminPrincipal = Depends(require_admin_session),
                     db: AsyncSession = Depends(get_db),
                     settings: Settings = Depends(get_settings)) -> Response:
    org = await organizations_repo.get_org(db, body.organization_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organization not found")
    m = await memberships_repo.get_membership(db, principal.email, org.id)
    if m is not None:
        role, acting_as = m.role, False
    elif principal.is_super_admin:
        role, acting_as = AdminRole.ADMIN, True
        await set_tenant_context(db, org.id)  # act-as audit lands in the target org
        await admin_audit.record(db, actor_email=principal.email, action="auth.act_as",
                                 entity_type="organization", entity_id=str(org.id))
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to this organization")
    await admin_users_repo.set_last_active_org(db, email=principal.email, org_id=org.id)
    await db.commit()
    out = MeResponse(email=principal.email, is_super_admin=principal.is_super_admin,
                     acting_as=acting_as,
                     active_org=OrgSummary(id=org.id, name=org.name, slug=org.slug,
                                           role=role.value if not acting_as else None),
                     orgs=[])  # client refetches /me for the full list
    resp = JSONResponse(out.model_dump(mode="json"))
    set_session_cookie(resp, issue_session(
        principal.email, active_org_id=org.id, role=role,
        is_super_admin=principal.is_super_admin, acting_as=acting_as, settings=settings), settings)
    return resp
```

- [ ] **Step 7 — Run, expect PASS.**
- [ ] **Step 8 — Commit:** `git commit -am "feat(api): org-scoped login, /me, switch-org + act-as (P2-B)"`

### Task B4: swap admin routers to `get_tenant_db`

**Files:** Swap `Depends(get_db)` → `Depends(get_tenant_db)` in the org-scoped admin routers ONLY:
`admin_profiles.py`, `admin_defaults.py`, `admin_profile_tests.py`, `admin_audit.py`, `admin_contacts.py`, `admin_family.py`, `admin_variable_catalog.py`, `admin_custom_variables.py`, `admin_tool_catalog.py`, `admin_voice_catalog.py`, `admin_model_catalog.py`, `admin_tools.py`, `admin_calls.py`. (`admin_users.py` is replaced in Unit C; `contacts.py`/`dnc.py`/`schedules.py`/`batches.py`/`calls.py`/`tools.py`/`runtime.py`/`webhooks.py` are operator/service/runtime — leave on `get_db`. The `auth.py` router stays on `get_db`.)

- [ ] **Step 1 — Failing test** (`apps/api/tests/test_admin_routes_org_scoped.py`): seed a contact in org A; an admin whose active org is B sees an empty list from `GET /v1/admin/contacts`; with active org A returns it. (Depends on Unit D fixtures.)
- [ ] **Step 2 — Run, expect FAIL** (routes use default-org `get_db` today).
- [ ] **Step 3 — Mechanical swap** in each file: add `from usan_api.auth import get_tenant_db` and replace each `db: AsyncSession = Depends(get_db)` → `Depends(get_tenant_db)`; drop the now-unused `get_db` import if nothing else uses it. Then verify: `grep -rn "Depends(get_db)" src/usan_api/routers/admin_*.py` returns nothing except `admin_users.py` (replaced in C).
- [ ] **Step 4 — Run, expect PASS.** Run the full admin suite to catch fixtures that must now provide an active org.
- [ ] **Step 5 — Commit:** `git commit -am "feat(api): admin routers resolve org from the session (get_tenant_db) (P2-B)"`

---

## UNIT C — Memberships API + org-create

### Task C1: replace `admin_users` router with a members router

**Files:** Delete `apps/api/src/usan_api/routers/admin_users.py`; create `apps/api/src/usan_api/routers/admin_members.py` + `apps/api/src/usan_api/schemas/members.py`. Update `main.py`. Test: `apps/api/tests/test_admin_members.py`.

- [ ] **Step 1 — Failing tests:** org ADMIN lists/adds/role-changes/removes members of the **active org** only; VIEWER gets 403 on writes; removing the last admin → 409; cannot see another org's members (the active org comes from the principal, not the URL).
- [ ] **Step 2 — Run, expect FAIL.**
- [ ] **Step 3 — Write `admin_members.py`** (org-scoped via `get_tenant_db`):
```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import AdminPrincipal, get_tenant_db, require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.repositories import admin_audit
from usan_api.repositories import memberships as repo
from usan_api.schemas.members import MemberCreate, MemberOut, MemberRoleUpdate

router = APIRouter(prefix="/v1/admin/members", tags=["members"],
                   dependencies=[Depends(require_admin_session)])


def _out(m) -> MemberOut:
    return MemberOut(email=m.email, role=m.role.value, added_by=m.added_by)


@router.get("", response_model=list[MemberOut])
async def list_members(principal: AdminPrincipal = Depends(require_admin_session),
                       db: AsyncSession = Depends(get_tenant_db)) -> list[MemberOut]:
    return [_out(m) for m in await repo.list_members(db, principal.active_org_id)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=MemberOut)
async def add_member(body: MemberCreate,
                     principal: AdminPrincipal = Depends(require_admin_session),
                     db: AsyncSession = Depends(get_tenant_db),
                     actor: str = Depends(get_actor_email),
                     _: object = Depends(require_admin_role(AdminRole.ADMIN))) -> MemberOut:
    m = await repo.add_member(db, email=body.email, org_id=principal.active_org_id,
                              role=AdminRole(body.role), added_by=actor)
    await admin_audit.record(db, actor_email=actor, action="member.add",
                             entity_type="membership", entity_id=m.email,
                             detail={"role": body.role})
    await db.commit()
    return _out(m)


@router.patch("/{email}", response_model=MemberOut)
async def set_role(email: str, body: MemberRoleUpdate,
                   principal: AdminPrincipal = Depends(require_admin_session),
                   db: AsyncSession = Depends(get_tenant_db),
                   actor: str = Depends(get_actor_email),
                   _: object = Depends(require_admin_role(AdminRole.ADMIN))) -> MemberOut:
    try:
        m = await repo.set_member_role(db, email=email, org_id=principal.active_org_id,
                                       role=AdminRole(body.role))
    except repo.LastOrgAdminError as e:
        await db.rollback(); raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found") from e
    await admin_audit.record(db, actor_email=actor, action="member.role",
                             entity_type="membership", entity_id=email.lower(),
                             detail={"role": body.role})
    await db.commit()
    return _out(m)


@router.delete("/{email}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(email: str,
                        principal: AdminPrincipal = Depends(require_admin_session),
                        db: AsyncSession = Depends(get_tenant_db),
                        actor: str = Depends(get_actor_email),
                        _: object = Depends(require_admin_role(AdminRole.ADMIN))) -> None:
    try:
        removed = await repo.remove_member(db, email=email, org_id=principal.active_org_id)
    except repo.LastOrgAdminError as e:
        await db.rollback(); raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    await admin_audit.record(db, actor_email=actor, action="member.remove",
                             entity_type="membership", entity_id=email.lower())
    await db.commit()
```
Create `apps/api/src/usan_api/schemas/members.py`:
```python
from pydantic import BaseModel, Field
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

class MemberOut(BaseModel):
    email: str
    role: str
    added_by: str | None = None

class MemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL)
    role: str = Field(default="admin", pattern="^(admin|viewer)$")

class MemberRoleUpdate(BaseModel):
    role: str = Field(pattern="^(admin|viewer)$")
```

- [ ] **Step 4 — `main.py`:** replace the `admin_users` import + `include_router(admin_users.router)` with `admin_members`.
- [ ] **Step 5 — Run, expect PASS. Commit.**

### Task C2: super-admin org console (`/v1/admin/organizations`)

**Files:** Create `apps/api/src/usan_api/routers/admin_organizations.py` + `schemas/organizations.py`. Update `main.py`. Test: `apps/api/tests/test_admin_organizations.py`.

- [ ] **Step 1 — Failing tests:** super-admin lists all orgs + creates an org (+ optional first member as ADMIN); non-super-admin → 403; duplicate slug → 409.
- [ ] **Step 2 — Run, expect FAIL.**
- [ ] **Step 3 — Write the router** (global tables → `require_super_admin` + `get_db`, NOT `get_tenant_db`):
```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import AdminPrincipal, require_super_admin
from usan_api.db.base import AdminRole
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import memberships as members_repo
from usan_api.repositories import organizations as orgs_repo
from usan_api.schemas.organizations import OrgCreate, OrgOut

router = APIRouter(prefix="/v1/admin/organizations", tags=["organizations"],
                   dependencies=[Depends(require_super_admin)])


@router.get("", response_model=list[OrgOut])
async def list_orgs(db: AsyncSession = Depends(get_db)) -> list[OrgOut]:
    return [OrgOut(id=o.id, name=o.name, slug=o.slug, status=o.status)
            for o in await orgs_repo.list_orgs(db)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OrgOut)
async def create_org(body: OrgCreate, principal: AdminPrincipal = Depends(require_super_admin),
                     db: AsyncSession = Depends(get_db)) -> OrgOut:
    try:
        org = await orgs_repo.create_org(db, name=body.name, slug=body.slug)
        await db.flush()
        if body.first_admin_email:
            await members_repo.add_member(db, email=body.first_admin_email, org_id=org.id,
                                          role=AdminRole.ADMIN, added_by=principal.email)
        await admin_audit.record(db, actor_email=principal.email, action="org.create",
                                 entity_type="organization", entity_id=str(org.id),
                                 detail={"slug": body.slug})
        await db.commit()
    except IntegrityError as e:
        await db.rollback(); raise HTTPException(status.HTTP_409_CONFLICT, "slug already exists") from e
    return OrgOut(id=org.id, name=org.name, slug=org.slug, status=org.status)
```
Create `apps/api/src/usan_api/schemas/organizations.py`:
```python
import uuid
from pydantic import BaseModel, Field
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str

class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern="^[a-z0-9-]{2,40}$")
    first_admin_email: str | None = Field(default=None, pattern=_EMAIL)
```
> The `org.create` audit row writes under the default-org `get_db` context (a platform-level super-admin action). The `admin_audit_log` is RLS-scoped; the connect baseline org is acceptable here.

- [ ] **Step 4 — `main.py` include. Run, expect PASS. Commit.**

---

## UNIT D — Per-org uniqueness + tests under `usan_app` + multi-org/act-as suite

### Task D1: migration 0034 — composite per-org uniqueness

**Files:** Create `apps/api/migrations/versions/0034_per_org_uniqueness.py`. Modify the `unique=True` flags in `models.py` (set `unique=False` + add a composite `UniqueConstraint` in `__table_args__`). Test: `apps/api/tests/test_per_org_uniqueness.py`.

- [ ] **Step 1 — Failing test** (same natural key in two orgs allowed; duplicate within one org rejected — using the usan_app two-org fixtures):
```python
import pytest
import sqlalchemy as sa
# Insert a contact with phone '+15551112222' into org A and org B (both succeed),
# then a second '+15551112222' into org A and assert it raises an IntegrityError.
```

- [ ] **Step 2 — Run, expect FAIL.**
- [ ] **Step 3 — Write `0034_per_org_uniqueness.py`.** For each `(table, column)` below: drop the single-column unique, add `UNIQUE(column, organization_id)`. The P1-era single-column uniques were auto-named `<table>_<column>_key`; confirm exact names with `psql \d <table>` (or `SELECT conname FROM pg_constraint`) and use those in `op.drop_constraint(name, table, type_="unique")`.
```python
_PER_ORG_UNIQUE = [
    ("contacts", "phone_e164"),
    ("contacts", "external_id"),
    ("agent_profiles", "name"),
    ("custom_variables", "name"),
    ("calls", "idempotency_key"),
    ("call_batches", "idempotency_key"),
    ("sms_messages", "dedupe_key"),
]
# upgrade(): for t,c: op.drop_constraint(f"{t}_{c}_key", t, type_="unique")
#            op.create_unique_constraint(f"uq_{t}_{c}_org", t, [c, "organization_id"])
# downgrade(): reverse (drop uq_, recreate single-column unique).
```
> EXCLUDED (provider-global / UUID — no cross-org collision, leave global): `sms_messages.telnyx_message_id`, `family_tasks.inbound_message_id`, `conversation_summaries` (its unique is on the `call_id` UUID), `organizations.slug`.
> Nullable columns (`external_id`, `idempotency_key`, `dedupe_key`): a composite UNIQUE still allows multiple NULLs per org (Postgres treats NULLs as distinct), preserving today's behavior.

- [ ] **Step 4 — Update `models.py`** for each of the 7: set `unique=False` on the column and add `UniqueConstraint(col, "organization_id", name=f"uq_{table}_{col}_org")` to that model's `__table_args__` (add a `__table_args__` tuple where none exists). Import `UniqueConstraint` from sqlalchemy.
- [ ] **Step 5 — Run + round-trip migration. Commit.**

### Task D2: run functional tests as `usan_app` with org context + multi-org fixtures

**Files:** Modify `apps/api/tests/conftest.py`. This is the deferred-HIGH item: `get_tenant_db` (and the seeding path) must run under `usan_app` + RLS with a test-chosen active org.

- [ ] **Step 1 — Add an async usan_app DSN fixture:**
```python
@pytest.fixture(scope="session")
def app_async_database_url(app_database_url: str) -> str:
    return app_database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
```

- [ ] **Step 2 — Add `two_orgs` + `app_session` fixtures.** `two_orgs` inserts org A + org B via the superuser engine and yields `(org_a_id, org_b_id)`, deleting them + their memberships on teardown. `app_session` opens a `usan_app` async session (from `app_async_database_url`, `NullPool`) for repo-level tests; tests that need org scoping call `set_tenant_context(session, org_id)` themselves.

- [ ] **Step 3 — Override `get_tenant_db` in `client`/`sso_client`.** Today these override only `get_db` (superuser, no context). Add an override for `usan_api.auth.get_tenant_db` that opens a `usan_app` session (a per-test `app_async` engine) and `set_tenant_context` to a mutable test-controlled active org (default: the seeded `usan`/active org). Provide an `act_as_org(app, org_id)` helper that updates which org the override scopes to. Keep the superuser `get_db` override for non-admin/runtime routes and for seeding.
> This will surface tests that seed as superuser into the default org but read through an admin route now scoped to a different active org. Fix each by seeding into the active org or aligning the test's active org with the seed. **This is the bulk of Unit D — iterate until green.**

- [ ] **Step 4 — Update the `admin_session` fixture** to also create a `Membership` for the seeded admin (so the principal resolves an active org) and to mint the cookie via the new `issue_session(..., active_org_id=<org>, role=AdminRole.ADMIN, is_super_admin=False, acting_as=False)`. Add a `super_admin_session` fixture (identity `is_super_admin=True`, no membership). Add `memberships` to `_TRUNCATE_ALL` (before `admin_users` for FK order).
- [ ] **Step 5 — Run the FULL api suite** (`cd apps/api && uv run pytest -q -n auto`) and drive to green. Commit.

### Task D3: multi-org + act-as isolation tests

**Files:** Create `apps/api/tests/test_rls_p2_isolation.py`.

- [ ] **Step 1 — Write tests** (assert behavior built in B/C/D; no new implementation): two-org admin-route isolation; act-as full-write lands in the target org's data + an audit row with the real email + `acting_as`; deleting a membership revokes on the next request; `409` for super-admin with no active org on an org-scoped route; cross-org members API blocked.
- [ ] **Step 2 — Run, expect PASS** (fix any gap in B/C). Commit.

---

## UNIT E — Admin UI (PR 2)

**Tech:** `apps/admin-ui` (React/TS, vitest + msw). Follow the existing `ContactsPage.tsx`/`hooks.ts`/`types/api.ts` patterns (PR #98).

### Task E1: types + `/me` + switch-org hooks
**Files:** `apps/admin-ui/src/types/api.ts`, the data-layer (`src/hooks.ts` or equivalent). Test: co-located vitest.
- [ ] Add `Org`/`Me` types (mirror `MeResponse`/`OrgSummary`); a `useMe()` query (`GET /v1/auth/me`) and a `useSwitchOrg()` mutation (`POST /v1/auth/switch-org`) that invalidates all org-scoped queries on success. TDD with the existing vitest+msw setup. Commit.

### Task E2: org switcher + act-as banner
**Files:** new `src/components/OrgSwitcher.tsx`, `src/components/ActingAsBanner.tsx`; mount in the app shell/nav.
- [ ] Switcher shows the active org + a dropdown of `me.orgs`; super-admins get an "Act as…" entry opening an org picker fed by `GET /v1/admin/organizations`. Selecting calls `useSwitchOrg`. `ActingAsBanner` renders a persistent banner when `me.acting_as` with an "Exit" control (switch to a membership org). vitest: banner only when `acting_as`; switch fires the mutation. Commit.

### Task E3: members management page
**Files:** new `src/pages/MembersPage.tsx` + route; hooks for `/v1/admin/members` CRUD.
- [ ] Table of members (email, role); add-by-email form; role `<select>`; remove with confirm; surface 409 (last admin) and 403 (viewer) inline. Gate writes on `me.active_org?.role === "admin"`. vitest for table + add/remove + error rendering. Commit.

### Task E4: super-admin org console
**Files:** new `src/pages/OrganizationsPage.tsx` + route (shown only when `me.is_super_admin`).
- [ ] List orgs (`GET /v1/admin/organizations`); "Create organization" form (name, slug, optional first-admin email) → `POST`; surface duplicate-slug 409. "Act as" button per row → `useSwitchOrg`. vitest. Commit.

---

## Self-Review (completed by plan author)

**Spec coverage:** D1–D6 → A1–A3/B1–B4. Data model → A1/A2. Auth/session → B1–B3. Org-resolution seam + runtime boundary → B2 (`get_tenant_db`) / B4 (only admin routers swapped). Authz/act-as → B2/B3. Migration & bootstrap → A2/A3. Per-org uniqueness + tests-under-usan_app → D1/D2. UI → E1–E4. Error codes (401/403/404/409) → B2/B3/C1/C2 tests. No gaps.

**Placeholder scan:** the only deferrals are deliberate verification steps (exact Postgres auto-constraint names in D1; the iterative test-fixup in D2), each with the concrete command to resolve them — not vague "handle errors" placeholders.

**Type consistency:** `AdminPrincipal(email, active_org_id, role, is_super_admin, acting_as)`, `issue_session(email, *, active_org_id, role, is_super_admin, acting_as, settings)`, and the repo signatures are used identically across A/B/C/D.

## Risks
- **D2 is the highest-effort task** (re-pointing functional tests at `usan_app`+RLS will surface seed/context mismatches). Budget iteration; keep each fix small.
- **Router-swap (B4) breadth** — verify no swapped file also serves a non-admin route; the `grep` step guards this.
- **Behavior-preserving in prod** until the `DATABASE_URL → usan_app` cutover; the P1 `_check_rls_role_capability()` startup guard still warns if the live role bypasses RLS.
- **Bootstrap import cycle** — `seed_bootstrap` calling `memberships.add_member` must import `memberships` lazily (it imports `admin_users`).
