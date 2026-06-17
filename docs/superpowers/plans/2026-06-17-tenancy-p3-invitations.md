# P3 — Org Invitations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an org ADMIN invite a person by email into their org via a pending-token invitation with a copyable accept link; the invitee signs in with Google and (on an exact email match) is added to the org.

**Architecture:** A new global, non-RLS `invitations` control-plane table (joined to `admin_users`/`memberships`/`organizations`). Invite-management API scoped to the caller's active org via `get_tenant_db` + ADMIN. The accept flow is an invite-aware OAuth bounce: a short-lived signed invite cookie rides through Google login; the `/v1/auth/callback` consumes the invite (token + exact Google-email match) and creates the identity + membership — bypassing the bootstrap allowlist for brand-new invitees. Copyable-link delivery (no email provider).

**Tech Stack:** FastAPI + SQLAlchemy 2.x async + Alembic (Python 3.14, uv); PyJWT HS256 cookies; Postgres (RLS for PHI tables, app-scoped for control-plane); React + react-query v5 + react-router (admin-ui).

**Spec:** `docs/superpowers/specs/2026-06-17-tenancy-p3-invitations-design.md`

---

## Execution Ordering (read before starting)

Tasks reference symbols defined in earlier tasks. Implement **in order**. Key dependencies:

- **A1** (`InviteStatus` enum) → **A2** (model) → **A3** (migration) → **A6** (repo) — the repo imports the model + enum, and repo tests need the migrated table.
- **A7** adds `invitations` to the conftest `_TRUNCATE_ALL` list. This MUST land before any **client-fixture** test runs (Units B, C, E) or rows leak across tests. Do it in Unit A.
- **B** (management API) imports the **A5** `build_accept_url` helper, the **A6** repo, and the **B1** schemas.
- **C** (accept flow) imports the **C1** invite-cookie helpers, the **A6** repo, and the **A5/C2** URL helpers.
- **D** (UI) is independent of B/C at runtime but exercises the same endpoints; build it after B/C so the contract is final.
- **E** is the dedicated cross-org isolation/RBAC proof using the `usan_app` (non-superuser) role.

Per-task TDD: write the failing test → run it red → implement → run it green → commit. Run `cd apps/api && uv run pytest -q` (and `ruff check . && ruff format . && uv run mypy`) before each backend commit; `cd apps/admin-ui && npm test && npm run lint && npm run typecheck` before each UI commit. CI runs mypy — do not skip it locally.

---

## Unit A — Schema, model, migration, settings, repo

### Task A1: `InviteStatus` enum

**Files:**
- Modify: `apps/api/src/usan_api/db/base.py`

- [ ] **Step 1: Add the enum** (mirrors the existing `AdminRole`/`ProfileStatus` style — lowercase `.value`s)

In `apps/api/src/usan_api/db/base.py`, after the `AdminRole` class, add:

```python
class InviteStatus(enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
```

- [ ] **Step 2: Commit**

```bash
git add apps/api/src/usan_api/db/base.py
git commit -m "feat(api): add InviteStatus enum (P3)"
```

---

### Task A2: `Invitation` ORM model

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py`

- [ ] **Step 1: Import the new enum**

In `apps/api/src/usan_api/db/models.py`, extend the existing base import (currently `from usan_api.db.base import AdminRole, Base, CallDirection, CallStatus, ProfileStatus`) to include `InviteStatus`:

```python
from usan_api.db.base import AdminRole, Base, CallDirection, CallStatus, InviteStatus, ProfileStatus
```

- [ ] **Step 2: Add the model** (place it next to `AdminUser`/`Membership`, near line 560). It is **global / non-RLS** — it does NOT use the `TenantScoped` mixin, and it declares its own `organization_id` FK + timestamps inline (there is no TimestampMixin in this codebase).

```python
class Invitation(Base):
    """Pending-token org invitation (P3). Global, non-RLS control-plane table —
    looked up by token before the accepter has any org context; app code scopes
    management queries by organization_id (the guard that replaces RLS here)."""

    __tablename__ = "invitations"
    __table_args__ = (
        # At most one LIVE invite per email per org; re-inviting regenerates this row.
        Index(
            "uq_invitations_org_email_pending",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_invitations_organization_id", "organization_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        SAEnum(AdminRole, name="admin_role", values_callable=_enum_values, create_type=False),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[InviteStatus] = mapped_column(
        SAEnum(
            InviteStatus, name="invite_status", values_callable=_enum_values, create_type=False
        ),
        nullable=False,
        server_default=InviteStatus.PENDING.value,
    )
    invited_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 3: Verify it imports**

Run: `cd apps/api && uv run python -c "from usan_api.db.models import Invitation; print(Invitation.__tablename__)"`
Expected: prints `invitations` (no errors).

- [ ] **Step 4: Commit**

```bash
git add apps/api/src/usan_api/db/models.py
git commit -m "feat(api): add Invitation ORM model (P3)"
```

---

### Task A3: Migration `0035` (create `invitations` + `invite_status` enum + grant)

**Files:**
- Create: `apps/api/migrations/versions/0035_invitations.py`

The current head is revision `"0034"` (file `0034_per_org_uniqueness.py`); chain onto it. `admin_role` already exists (migration 0010) — reference it with `create_type=False` and do NOT re-create it. Create the new `invite_status` type explicitly. Grant the non-superuser `usan_app` role access (or the app cannot use the table after the prod cutover).

- [ ] **Step 1: Write the migration**

```python
"""P3: invitations table (pending-token org invites). Global, non-RLS control-plane
table joined to organizations; looked up by token before any org context exists.

Revision ID: 0035
Revises: 0034
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New enum type. admin_role already exists (0010) — referenced with create_type=False.
    invite_status = postgresql.ENUM(
        "pending", "accepted", "revoked", name="invite_status", create_type=False
    )
    invite_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("admin", "viewer", name="admin_role", create_type=False),
            nullable=False,
        ),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("status", invite_status, nullable=False, server_default="pending"),
        sa.Column("invited_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_invitations_token", "invitations", ["token"], unique=True)
    op.create_index("ix_invitations_organization_id", "invitations", ["organization_id"])
    op.create_index(
        "uq_invitations_org_email_pending",
        "invitations",
        ["organization_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # The non-superuser app role (RLS subject) must be able to use the table.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON invitations TO usan_app")


def downgrade() -> None:
    op.drop_index("uq_invitations_org_email_pending", table_name="invitations")
    op.drop_index("ix_invitations_organization_id", table_name="invitations")
    op.drop_index("uq_invitations_token", table_name="invitations")
    op.drop_table("invitations")
    postgresql.ENUM(name="invite_status", create_type=False).drop(op.get_bind(), checkfirst=True)
```

- [ ] **Step 2: Apply it**

Run: `cd apps/api && uv run alembic upgrade head`
Expected: applies `0035` with no error; `uv run alembic current` shows `0035 (head)`.

- [ ] **Step 3: Round-trip check (down then up)**

Run: `cd apps/api && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: downgrade drops the table + enum cleanly, upgrade re-applies — no error.

- [ ] **Step 4: Commit**

```bash
git add apps/api/migrations/versions/0035_invitations.py
git commit -m "feat(api): migration 0035 — invitations table + invite_status enum (P3)"
```

---

### Task A4: Settings — `invite_ttl_hours` + `admin_base_url`

**Files:**
- Modify: `apps/api/src/usan_api/settings.py`
- Test: `apps/api/tests/test_settings.py` (extend if present; otherwise create)

- [ ] **Step 1: Write the failing test**

Add to `apps/api/tests/test_settings.py` (mirror the existing settings-test style — these tests construct `Settings(...)` with the minimal required env; check an existing test for the required-field kwargs and copy them):

```python
def test_invite_settings_defaults_and_validation(monkeypatch):
    from usan_api.settings import Settings, get_settings

    get_settings.cache_clear()
    # ... set the same minimal required env an existing Settings test uses ...
    s = Settings()  # use the project's existing minimal-construction helper if there is one
    assert s.invite_ttl_hours == 168
    assert s.admin_base_url is None
```

(If `test_settings.py` builds `Settings` via a fixture/helper, reuse it instead of `Settings()` — read the file first and match its construction pattern.)

- [ ] **Step 2: Run it red**

Run: `cd apps/api && uv run pytest tests/test_settings.py -q -k invite`
Expected: FAIL (`invite_ttl_hours` attribute missing).

- [ ] **Step 3: Add the settings fields**

In `apps/api/src/usan_api/settings.py`, near `admin_post_login_redirect` (line ~75), add:

```python
    invite_ttl_hours: int = Field(default=168, ge=1, le=720, alias="INVITE_TTL_HOURS")
    # Absolute public origin of the admin app, used to build invite accept links. When
    # unset, the origin is derived from GOOGLE_OAUTH_REDIRECT_URI (already configured for
    # SSO) — so prod needs no new env var. If set, must be absolute (http(s)://host).
    admin_base_url: str | None = Field(default=None, alias="ADMIN_BASE_URL")
```

And add a validator next to the existing `_relative_redirect` validator:

```python
    @field_validator("admin_base_url")
    @classmethod
    def _absolute_base_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("ADMIN_BASE_URL must be absolute (http:// or https://)")
        return v.rstrip("/") if v else v
```

- [ ] **Step 4: Run it green**

Run: `cd apps/api && uv run pytest tests/test_settings.py -q -k invite`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/settings.py apps/api/tests/test_settings.py
git commit -m "feat(api): invite_ttl_hours + admin_base_url settings (P3)"
```

---

### Task A5: `build_accept_url` helper

**Files:**
- Create: `apps/api/src/usan_api/invites.py`
- Test: `apps/api/tests/test_invites_url.py`

- [ ] **Step 1: Write the failing test**

```python
from usan_api.invites import build_accept_error_url, build_accept_url


class _S:
    """Minimal Settings stand-in for the URL builders."""

    def __init__(self, admin_base_url=None, google_oauth_redirect_uri=None):
        self.admin_base_url = admin_base_url
        self.google_oauth_redirect_uri = google_oauth_redirect_uri


def test_accept_url_prefers_admin_base_url():
    s = _S(admin_base_url="https://admin.example.com")
    assert build_accept_url(s, "tok123") == (
        "https://admin.example.com/v1/auth/accept-invite?token=tok123"
    )


def test_accept_url_falls_back_to_oauth_redirect_origin():
    s = _S(google_oauth_redirect_uri="https://admin.example.com/v1/auth/callback")
    assert build_accept_url(s, "tok123") == (
        "https://admin.example.com/v1/auth/accept-invite?token=tok123"
    )


def test_accept_error_url():
    s = _S(admin_base_url="https://admin.example.com")
    assert build_accept_error_url(s, "mismatch") == (
        "https://admin.example.com/accept-invite?status=error&reason=mismatch"
    )
```

- [ ] **Step 2: Run it red**

Run: `cd apps/api && uv run pytest tests/test_invites_url.py -q`
Expected: FAIL (`usan_api.invites` does not exist).

- [ ] **Step 3: Implement**

```python
"""Invite-link URL construction (P3). The accept link points at the API accept
endpoint (served from the same public origin as the SPA via Caddy's /v1 proxy)."""

from urllib.parse import quote, urlsplit

from usan_api.settings import Settings


def _origin(settings: Settings) -> str:
    base = settings.admin_base_url or settings.google_oauth_redirect_uri or ""
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}"


def build_accept_url(settings: Settings, token: str) -> str:
    """The link an admin copies; opening it bounces through Google OAuth to accept."""
    return f"{_origin(settings)}/v1/auth/accept-invite?token={quote(token, safe='')}"


def build_accept_error_url(settings: Settings, reason: str) -> str:
    """Where the callback redirects the browser when an invite can't be accepted."""
    return f"{_origin(settings)}/accept-invite?status=error&reason={quote(reason, safe='')}"
```

- [ ] **Step 4: Run it green**

Run: `cd apps/api && uv run pytest tests/test_invites_url.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/invites.py apps/api/tests/test_invites_url.py
git commit -m "feat(api): invite accept-URL builders (P3)"
```

---

### Task A6: `invitations` repository

**Files:**
- Create: `apps/api/src/usan_api/repositories/invitations.py`
- Test: `apps/api/tests/test_invitations_repo.py`

The repo is the only place that touches the `invitations` table. `invitations` is global/non-RLS; `create_invite` rejects inviting an existing member (it imports the memberships repo). Tests run on the `app_session` (usan_app, non-superuser) fixture — no org context needed for these global tables.

- [ ] **Step 1: Write the failing tests**

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from usan_api.db.base import AdminRole, InviteStatus
from usan_api.repositories import invitations as repo
from usan_api.repositories import memberships as memberships_repo

# Fixtures `two_orgs`, `app_session` come from conftest (P2 additions).


async def test_create_and_list_pending(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session, org_id=org_a, email="A@X.com", role=AdminRole.VIEWER,
        invited_by="boss@x.com", ttl_hours=168,
    )
    assert inv.email == "a@x.com"  # normalized
    assert inv.status is InviteStatus.PENDING
    assert inv.token  # non-empty
    assert inv.expires_at > datetime.now(UTC)
    pending = await repo.list_pending(app_session, org_a)
    assert [i.email for i in pending] == ["a@x.com"]


async def test_create_regenerates_existing_pending(two_orgs, app_session):
    org_a, _ = two_orgs
    first = await repo.create_invite(
        app_session, org_id=org_a, email="a@x.com", role=AdminRole.VIEWER,
        invited_by="b@x.com", ttl_hours=168,
    )
    first_token = first.token
    second = await repo.create_invite(
        app_session, org_id=org_a, email="a@x.com", role=AdminRole.ADMIN,
        invited_by="b@x.com", ttl_hours=168,
    )
    assert second.id == first.id  # same row, regenerated
    assert second.token != first_token
    assert len(await repo.list_pending(app_session, org_a)) == 1


async def test_create_rejects_existing_member(two_orgs, app_session):
    org_a, _ = two_orgs
    await memberships_repo.add_member(
        app_session, email="m@x.com", org_id=org_a, role=AdminRole.VIEWER, added_by="t"
    )
    with pytest.raises(repo.AlreadyMemberError):
        await repo.create_invite(
            app_session, org_id=org_a, email="m@x.com", role=AdminRole.ADMIN,
            invited_by="b@x.com", ttl_hours=168,
        )


async def test_get_invite_is_org_scoped(two_orgs, app_session):
    org_a, org_b = two_orgs
    inv = await repo.create_invite(
        app_session, org_id=org_a, email="a@x.com", role=AdminRole.VIEWER,
        invited_by="b@x.com", ttl_hours=168,
    )
    assert await repo.get_invite(app_session, inv.id, org_a) is not None
    assert await repo.get_invite(app_session, inv.id, org_b) is None  # other org can't see it


async def test_get_by_token(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session, org_id=org_a, email="a@x.com", role=AdminRole.VIEWER,
        invited_by="b@x.com", ttl_hours=168,
    )
    found = await repo.get_by_token(app_session, inv.token)
    assert found is not None and found.id == inv.id
    assert await repo.get_by_token(app_session, "nope") is None


async def test_revoke(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session, org_id=org_a, email="a@x.com", role=AdminRole.VIEWER,
        invited_by="b@x.com", ttl_hours=168,
    )
    await repo.revoke(app_session, inv)
    assert inv.status is InviteStatus.REVOKED
    with pytest.raises(repo.NotPendingError):
        await repo.revoke(app_session, inv)  # not pending anymore


async def test_resend_rotates_token_and_expiry(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session, org_id=org_a, email="a@x.com", role=AdminRole.VIEWER,
        invited_by="b@x.com", ttl_hours=1,
    )
    old_token, old_exp = inv.token, inv.expires_at
    again = await repo.resend(app_session, inv, ttl_hours=168)
    assert again.token != old_token
    assert again.expires_at > old_exp


async def test_mark_accepted_and_usability(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session, org_id=org_a, email="a@x.com", role=AdminRole.VIEWER,
        invited_by="b@x.com", ttl_hours=168,
    )
    now = datetime.now(UTC)
    assert repo.is_usable(inv, now=now) is True
    await repo.mark_accepted(app_session, inv)
    assert inv.status is InviteStatus.ACCEPTED
    assert inv.accepted_at is not None
    assert repo.is_usable(inv, now=now) is False  # accepted -> not usable


def test_is_usable_false_when_expired():
    # Pure helper test (no DB): a pending-but-expired invite is not usable.
    from types import SimpleNamespace

    past = datetime.now(UTC) - timedelta(hours=1)
    inv = SimpleNamespace(status=InviteStatus.PENDING, expires_at=past)
    assert repo.is_usable(inv, now=datetime.now(UTC)) is False
```

- [ ] **Step 2: Run it red**

Run: `cd apps/api && uv run pytest tests/test_invitations_repo.py -q`
Expected: FAIL (`usan_api.repositories.invitations` does not exist).

- [ ] **Step 3: Implement the repo**

```python
"""Invitation data access (P3). The invitations table is GLOBAL (non-RLS), so the
management functions scope by organization_id in app code; accept looks up globally
by the unique token. created_at/id come from DB defaults (read back via refresh)."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import AdminRole, InviteStatus
from usan_api.db.models import Invitation
from usan_api.repositories import memberships as memberships_repo


class AlreadyMemberError(Exception):
    """Refuse inviting someone who is already a member of the org."""


class NotPendingError(Exception):
    """Revoke/resend attempted on an invite that is no longer pending."""


def _norm(email: str) -> str:
    return email.strip().lower()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def is_usable(invite: Invitation, *, now: datetime) -> bool:
    """An invite can be accepted iff it is pending and unexpired (lazy expiry)."""
    return invite.status is InviteStatus.PENDING and invite.expires_at > now


async def create_invite(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    role: AdminRole,
    invited_by: str | None,
    ttl_hours: int,
) -> Invitation:
    """Create (or regenerate the existing pending) invite for (org, email).

    Rejects inviting an existing member. The partial unique index
    (organization_id, email) WHERE status='pending' is the integrity backstop for the
    select-then-insert below.
    """
    norm = _norm(email)
    if await memberships_repo.get_membership(db, norm, org_id) is not None:
        raise AlreadyMemberError("already a member of this organization")
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
    existing = (
        await db.execute(
            select(Invitation).where(
                Invitation.organization_id == org_id,
                Invitation.email == norm,
                Invitation.status == InviteStatus.PENDING,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.token = _new_token()
        existing.expires_at = expires
        existing.role = role
        await db.flush()
        return existing
    invite = Invitation(
        organization_id=org_id,
        email=norm,
        role=role,
        token=_new_token(),
        status=InviteStatus.PENDING,
        invited_by=invited_by,
        expires_at=expires,
    )
    db.add(invite)
    await db.flush()
    await db.refresh(invite)  # id / created_at from DB defaults
    return invite


async def list_pending(db: AsyncSession, org_id: uuid.UUID) -> list[Invitation]:
    res = await db.execute(
        select(Invitation)
        .where(Invitation.organization_id == org_id, Invitation.status == InviteStatus.PENDING)
        .order_by(Invitation.created_at)
    )
    return list(res.scalars().all())


async def get_invite(
    db: AsyncSession, invite_id: uuid.UUID, org_id: uuid.UUID
) -> Invitation | None:
    """Scoped by org — an invite id from another org returns None (404 in the router)."""
    res = await db.execute(
        select(Invitation).where(
            Invitation.id == invite_id, Invitation.organization_id == org_id
        )
    )
    return res.scalar_one_or_none()


async def get_by_token(db: AsyncSession, token: str) -> Invitation | None:
    res = await db.execute(select(Invitation).where(Invitation.token == token))
    return res.scalar_one_or_none()


async def revoke(db: AsyncSession, invite: Invitation) -> None:
    if invite.status is not InviteStatus.PENDING:
        raise NotPendingError("invite is not pending")
    invite.status = InviteStatus.REVOKED
    await db.flush()


async def resend(db: AsyncSession, invite: Invitation, *, ttl_hours: int) -> Invitation:
    if invite.status is not InviteStatus.PENDING:
        raise NotPendingError("invite is not pending")
    invite.token = _new_token()
    invite.expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    await db.flush()
    return invite


async def mark_accepted(db: AsyncSession, invite: Invitation) -> None:
    invite.status = InviteStatus.ACCEPTED
    invite.accepted_at = datetime.now(UTC)
    await db.flush()
```

- [ ] **Step 4: Run it green**

Run: `cd apps/api && uv run pytest tests/test_invitations_repo.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Lint + types + commit**

```bash
cd apps/api && ruff check . && ruff format . && uv run mypy
git add apps/api/src/usan_api/repositories/invitations.py apps/api/tests/test_invitations_repo.py
git commit -m "feat(api): invitations repository (P3)"
```

---

### Task A7: Register `invitations` in the test truncate list

**Files:**
- Modify: `apps/api/tests/conftest.py`

The `client` fixture truncates a fixed table list before/after each test (`_TRUNCATE_ALL`, ~lines 231-242). New tables must be added or rows leak across client-fixture tests.

- [ ] **Step 1: Add the table**

Read `apps/api/tests/conftest.py` around lines 231-242, find the table-name list used by `_truncate` (it lists `memberships`, `admin_users`, etc.), and add `"invitations"` to it (place it before `memberships`/`admin_users` — `TRUNCATE ... CASCADE` handles FK order, but keep it tidy). Example edit (match the exact existing literal style):

```python
    "invitations",
    "memberships",
    "admin_users",
```

- [ ] **Step 2: Sanity-run an existing client test**

Run: `cd apps/api && uv run pytest tests/test_rls_p2_isolation.py -q`
Expected: PASS (truncate list still valid; no error referencing `invitations`).

- [ ] **Step 3: Commit**

```bash
git add apps/api/tests/conftest.py
git commit -m "test(api): truncate invitations between client tests (P3)"
```

---

## Unit B — Invite-management API

### Task B1: Invite schemas

**Files:**
- Create: `apps/api/src/usan_api/schemas/invites.py`

- [ ] **Step 1: Implement** (mirrors `schemas/members.py` — hand-rolled email regex, role pattern, no `EmailStr`)

```python
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

# Minimal email regex avoids the email-validator dependency EmailStr needs.
_EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class InviteCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=_EMAIL)
    role: str = Field(default="admin", pattern="^(admin|viewer)$")


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    status: str
    accept_url: str
    expires_at: datetime
    created_at: datetime
    invited_by: str | None = None
```

- [ ] **Step 2: Commit**

```bash
git add apps/api/src/usan_api/schemas/invites.py
git commit -m "feat(api): invite request/response schemas (P3)"
```

---

### Task B2: Invite-management router + mount

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_invites.py`
- Modify: `apps/api/src/usan_api/main.py` (import tuple ~lines 27-53; include block ~lines 220-245)
- Test: `apps/api/tests/test_admin_invites_api.py`

This mirrors `routers/admin_members.py` exactly: router-level `require_admin_session`, per-write `require_admin_role(AdminRole.ADMIN)`, org from `get_tenant_db`, explicit commit, `db.rollback()` on guarded errors, audit before commit. All endpoints (incl. GET) are ADMIN-only — invite links are not shown to viewers.

- [ ] **Step 1: Write the failing tests** (cookie-injection auth; `admin_session`/`super_admin_session` fixtures from conftest; `_member_cookie` pattern from `test_rls_p2_isolation.py`)

```python
import asyncio
import uuid

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings


def _member_cookie(email, org_id, role):
    token = issue_session(
        email, active_org_id=org_id, role=role, is_super_admin=False, acting_as=False,
        settings=get_settings(),
    )
    return {SESSION_COOKIE_NAME: token}


def test_create_invite_returns_accept_url(client, admin_session):
    r = client.post("/v1/admin/invites", json={"email": "new@x.com", "role": "viewer"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "new@x.com"
    assert body["role"] == "viewer"
    assert body["status"] == "pending"
    assert "/v1/auth/accept-invite?token=" in body["accept_url"]


def test_create_invite_idempotent_reinvite(client, admin_session):
    a = client.post("/v1/admin/invites", json={"email": "dup@x.com", "role": "admin"}).json()
    b = client.post("/v1/admin/invites", json={"email": "dup@x.com", "role": "viewer"}).json()
    assert a["id"] == b["id"]  # same row regenerated
    listed = client.get("/v1/admin/invites").json()
    assert [i["email"] for i in listed] == ["dup@x.com"]  # exactly one pending


def test_create_invite_rejects_existing_member(client, admin_session):
    # admin@example.com is already a member (the admin_session fixture seeds them).
    r = client.post("/v1/admin/invites", json={"email": "admin@example.com", "role": "viewer"})
    assert r.status_code == 409


def test_viewer_cannot_manage_invites(client, async_database_url):
    from tests.conftest import _seed_admin_user_async  # seeds identity + usan membership

    org_id = asyncio.run(_seed_admin_user_async(async_database_url, "view@example.com", "viewer"))
    cookie = _member_cookie("view@example.com", org_id, AdminRole.VIEWER)
    # ADMIN-only on every endpoint, including list.
    assert client.get("/v1/admin/invites", cookies=cookie).status_code == 403
    r = client.post("/v1/admin/invites", json={"email": "x@x.com"}, cookies=cookie)
    assert r.status_code == 403


def test_revoke_and_resend(client, admin_session):
    created = client.post("/v1/admin/invites", json={"email": "r@x.com", "role": "viewer"}).json()
    iid = created["id"]
    resent = client.post(f"/v1/admin/invites/{iid}/resend")
    assert resent.status_code == 200
    assert "/v1/auth/accept-invite?token=" in resent.json()["accept_url"]
    assert client.delete(f"/v1/admin/invites/{iid}").status_code == 204
    # revoking again -> 409 (not pending)
    assert client.delete(f"/v1/admin/invites/{iid}").status_code == 409
    assert client.get("/v1/admin/invites").json() == []  # no longer pending


def test_revoke_unknown_invite_404(client, admin_session):
    assert client.delete(f"/v1/admin/invites/{uuid.uuid4()}").status_code == 404
```

- [ ] **Step 2: Run it red**

Run: `cd apps/api && uv run pytest tests/test_admin_invites_api.py -q`
Expected: FAIL (router not mounted / module missing).

- [ ] **Step 3: Implement the router**

```python
"""Per-org invitation management (P3). Active org comes from the session principal
(get_tenant_db), never the URL. invitations is a GLOBAL (non-RLS) table, so every
query is scoped by principal.active_org_id in app code (see repositories/invitations).
All endpoints require ADMIN — invite links are not shown to viewers."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import (
    AdminPrincipal,
    get_tenant_db,
    require_admin_role,
    require_admin_session,
)
from usan_api.db.base import AdminRole
from usan_api.db.models import Invitation
from usan_api.invites import build_accept_url
from usan_api.repositories import admin_audit
from usan_api.repositories import invitations as repo
from usan_api.schemas.invites import InviteCreate, InviteOut
from usan_api.settings import Settings, get_settings

router = APIRouter(
    prefix="/v1/admin/invites",
    tags=["invites"],
    dependencies=[Depends(require_admin_session)],
)


def _out(inv: Invitation, settings: Settings) -> InviteOut:
    return InviteOut(
        id=inv.id,
        email=inv.email,
        role=inv.role.value,
        status=inv.status.value,
        accept_url=build_accept_url(settings, inv.token),
        expires_at=inv.expires_at,
        created_at=inv.created_at,
        invited_by=inv.invited_by,
    )


@router.get("", response_model=list[InviteOut])
async def list_invites(
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
    settings: Settings = Depends(get_settings),
) -> list[InviteOut]:
    assert principal.active_org_id is not None
    return [_out(i, settings) for i in await repo.list_pending(db, principal.active_org_id)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InviteOut)
async def create_invite(
    body: InviteCreate,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
    settings: Settings = Depends(get_settings),
) -> InviteOut:
    assert principal.active_org_id is not None
    try:
        inv = await repo.create_invite(
            db,
            org_id=principal.active_org_id,
            email=body.email,
            role=AdminRole(body.role),
            invited_by=actor,
            ttl_hours=settings.invite_ttl_hours,
        )
    except repo.AlreadyMemberError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "already a member of this organization") from e
    await admin_audit.record(
        db,
        actor_email=actor,
        action="invite.create",
        entity_type="invitation",
        entity_id=inv.email,
        detail={"role": body.role},
    )
    await db.commit()
    return _out(inv, settings)


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: uuid.UUID,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    assert principal.active_org_id is not None
    inv = await repo.get_invite(db, invite_id, principal.active_org_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    try:
        await repo.revoke(db, inv)
    except repo.NotPendingError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "invite is not pending") from e
    await admin_audit.record(
        db, actor_email=actor, action="invite.revoke", entity_type="invitation", entity_id=inv.email
    )
    await db.commit()


@router.post("/{invite_id}/resend", response_model=InviteOut)
async def resend_invite(
    invite_id: uuid.UUID,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
    settings: Settings = Depends(get_settings),
) -> InviteOut:
    assert principal.active_org_id is not None
    inv = await repo.get_invite(db, invite_id, principal.active_org_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    try:
        inv = await repo.resend(db, inv, ttl_hours=settings.invite_ttl_hours)
    except repo.NotPendingError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "invite is not pending") from e
    await admin_audit.record(
        db, actor_email=actor, action="invite.resend", entity_type="invitation", entity_id=inv.email
    )
    await db.commit()
    return _out(inv, settings)
```

- [ ] **Step 3b: Mount the router**

In `apps/api/src/usan_api/main.py`: add `admin_invites` to the alphabetized `from usan_api.routers import (...)` tuple (after `admin_family`), and add `app.include_router(admin_invites.router)` in the include block (after `app.include_router(admin_family.router)`):

```python
    app.include_router(admin_invites.router)
```

- [ ] **Step 4: Run it green**

Run: `cd apps/api && uv run pytest tests/test_admin_invites_api.py -q`
Expected: PASS.

- [ ] **Step 5: Lint + types + commit**

```bash
cd apps/api && ruff check . && ruff format . && uv run mypy
git add apps/api/src/usan_api/routers/admin_invites.py apps/api/src/usan_api/main.py apps/api/tests/test_admin_invites_api.py
git commit -m "feat(api): invite-management API — create/list/revoke/resend (P3)"
```

---

## Unit C — Accept flow (invite-aware OAuth)

### Task C1: Invite cookie helpers

**Files:**
- Modify: `apps/api/src/usan_api/admin_session.py`
- Test: `apps/api/tests/test_invite_cookie.py`

Mirror the **tx** cookie (Lax SameSite, scoped path, short TTL) — NOT the session cookie — because the invite cookie must survive Google's cross-site top-level redirect into `/callback`.

- [ ] **Step 1: Write the failing test**

```python
import jwt
import pytest

from usan_api.admin_session import (
    INVITE_COOKIE_NAME,
    decode_invite,
    issue_invite,
    issue_tx,
)
from usan_api.settings import get_settings


def _settings():
    # Reuse the project's minimal Settings construction (see other admin_session tests).
    import os

    os.environ.setdefault("JWT_SIGNING_KEY", "s" * 32)
    get_settings.cache_clear()
    return get_settings()


def test_invite_cookie_roundtrip():
    s = _settings()
    token = issue_invite("the-token", s)
    claims = decode_invite(token, s)
    assert claims["invite_token"] == "the-token"


def test_decode_invite_rejects_wrong_type():
    s = _settings()
    tx = issue_tx("state", "verifier", s)
    with pytest.raises(jwt.PyJWTError):
        decode_invite(tx, s)
```

> Read an existing `admin_session` test for the exact minimal-`Settings` construction and copy it rather than poking `os.environ` if a helper exists. `INVITE_COOKIE_NAME` is imported only to assert the module exposes it; if unused by an assertion, drop it to satisfy the linter.

- [ ] **Step 2: Run it red**

Run: `cd apps/api && uv run pytest tests/test_invite_cookie.py -q`
Expected: FAIL (`issue_invite`/`decode_invite` missing).

- [ ] **Step 3: Implement** — add to `apps/api/src/usan_api/admin_session.py`, alongside the tx constants/helpers:

```python
INVITE_COOKIE_NAME = "admin_invite_tx"
INVITE_PATH = "/v1/auth"
INVITE_TTL_S = 600  # 10 minutes: rides the OAuth round-trip, then dies.
```

```python
def issue_invite(token: str, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "invite_token": token,
        "typ": "oauth_invite",
        "iat": now,
        "exp": now + timedelta(seconds=INVITE_TTL_S),
    }
    return jwt.encode(payload, _key(settings), algorithm=_ALG)


def decode_invite(token: str, settings: Settings) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        token, _key(settings), algorithms=[_ALG], options={"require": ["exp", "invite_token"]}
    )
    if claims.get("typ") != "oauth_invite":
        raise jwt.InvalidTokenError("not an oauth-invite token")
    return claims


def set_invite_cookie(resp: Response, token: str, settings: Settings) -> None:
    resp.set_cookie(
        INVITE_COOKIE_NAME,
        token,
        max_age=INVITE_TTL_S,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path=INVITE_PATH,
    )


def clear_invite_cookie(resp: Response, settings: Settings) -> None:
    resp.delete_cookie(
        INVITE_COOKIE_NAME,
        path=INVITE_PATH,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
```

- [ ] **Step 4: Run it green**

Run: `cd apps/api && uv run pytest tests/test_invite_cookie.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/admin_session.py apps/api/tests/test_invite_cookie.py
git commit -m "feat(api): short-lived invite cookie helpers (P3)"
```

---

### Task C2: `accept-invite` endpoint + invite-aware callback

**Files:**
- Modify: `apps/api/src/usan_api/routers/auth.py`
- Test: `apps/api/tests/test_invite_accept_flow.py`

Before writing tests, **read `apps/api/tests/test_auth_flow_p2.py`** to reuse its established OAuth-mocking pattern (the `sso_client` fixture and the monkeypatching of `usan_api.oauth.exchange_code` / `usan_api.oauth.verify_id_token`, plus how it sets the `tx` cookie via `issue_tx`). The accept tests use the same machinery, additionally setting the invite cookie via `issue_invite`.

- [ ] **Step 1: Write the failing tests** (use the same `sso_client` + oauth-mock fixtures as `test_auth_flow_p2.py`)

```python
import asyncio
from datetime import UTC, datetime, timedelta

from usan_api.admin_session import SESSION_COOKIE_NAME, decode_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

# `sso_client`, the oauth monkeypatch, and `set_verified(email)` are reused from the
# pattern in tests/test_auth_flow_p2.py. `seed_invite(...)` inserts a pending invite row
# directly via the superuser engine (invitations is non-RLS) and returns its token;
# `invite_status(token)` and `membership_exists(email, org)` read back via that engine.


def _accept(sso_client, *, token, verified_email, set_verified):
    """accept-invite -> Google bounce -> callback, carrying the cookies across."""
    set_verified(verified_email)
    r = sso_client.get(f"/v1/auth/accept-invite?token={token}", follow_redirects=False)
    assert r.status_code == 302
    return sso_client.get("/v1/auth/callback?code=abc&state=STATE", follow_redirects=False)


def test_accept_brand_new_invitee(sso_client, two_orgs, set_verified):
    org_a, _ = two_orgs
    token = asyncio.run(seed_invite(org_a, "newbie@x.com", AdminRole.VIEWER))
    resp = _accept(sso_client, token=token, verified_email="newbie@x.com", set_verified=set_verified)
    assert resp.status_code == 303  # success -> post-login redirect
    session = resp.cookies.get(SESSION_COOKIE_NAME)
    assert session is not None
    claims = decode_session(session, get_settings())
    assert claims["sub"] == "newbie@x.com"
    assert claims["active_org"] == str(org_a)
    assert claims["role"] == "viewer"
    assert claims["acting_as"] is False
    assert asyncio.run(membership_exists("newbie@x.com", org_a)) is True
    assert asyncio.run(invite_status(token)) == "accepted"


def test_accept_email_mismatch_does_not_consume(sso_client, two_orgs, set_verified):
    org_a, _ = two_orgs
    token = asyncio.run(seed_invite(org_a, "intended@x.com", AdminRole.ADMIN))
    resp = _accept(sso_client, token=token, verified_email="someone-else@x.com", set_verified=set_verified)
    assert resp.status_code == 303
    assert "status=error&reason=mismatch" in resp.headers["location"]
    assert resp.cookies.get(SESSION_COOKIE_NAME) is None
    assert asyncio.run(invite_status(token)) == "pending"  # NOT consumed
    assert asyncio.run(membership_exists("someone-else@x.com", org_a)) is False


def test_accept_expired_invite(sso_client, two_orgs, set_verified):
    org_a, _ = two_orgs
    token = asyncio.run(
        seed_invite(org_a, "late@x.com", AdminRole.VIEWER, expires_at=datetime.now(UTC) - timedelta(hours=1))
    )
    resp = _accept(sso_client, token=token, verified_email="late@x.com", set_verified=set_verified)
    assert "status=error&reason=expired" in resp.headers["location"]
    assert asyncio.run(invite_status(token)) == "pending"


def test_accept_revoked_invite(sso_client, two_orgs, set_verified):
    org_a, _ = two_orgs
    token = asyncio.run(seed_invite(org_a, "rev@x.com", AdminRole.VIEWER, status="revoked"))
    resp = _accept(sso_client, token=token, verified_email="rev@x.com", set_verified=set_verified)
    assert "status=error&reason=revoked" in resp.headers["location"]


def test_accept_missing_token_400(sso_client):
    assert sso_client.get("/v1/auth/accept-invite", follow_redirects=False).status_code == 400
```

> Implement the helpers (`seed_invite`, `invite_status`, `membership_exists`, `set_verified`, and the `sso_client` fixture) in this test module by copying the conventions from `test_auth_flow_p2.py`. `seed_invite` inserts directly into `invitations` via the superuser engine (it is non-RLS) — columns: `organization_id, email, role (CAST AS admin_role), token (secrets.token_urlsafe), status (CAST AS invite_status, default 'pending'), invited_by, expires_at (default now()+ttl)` — and returns the token. The TestClient keeps cookies across the accept→callback calls, so the tx + invite cookies set by `accept-invite` are present on the `callback` request.

- [ ] **Step 2: Run it red**

Run: `cd apps/api && uv run pytest tests/test_invite_accept_flow.py -q`
Expected: FAIL (`/v1/auth/accept-invite` 404; no invite branch in callback).

- [ ] **Step 3: Implement — imports + accept endpoint**

In `apps/api/src/usan_api/routers/auth.py`, extend imports (add to the existing `admin_session` import group and add new ones):

```python
import uuid
from datetime import UTC, datetime

from usan_api.admin_session import (
    INVITE_COOKIE_NAME,
    clear_invite_cookie,
    decode_invite,
    issue_invite,
    set_invite_cookie,
)  # add alongside the existing SESSION_COOKIE_NAME / TX_COOKIE_NAME / issue_session imports
from usan_api.db.base import AdminRole, InviteStatus
from usan_api.db.models import Invitation
from usan_api.invites import build_accept_error_url
from usan_api.repositories import invitations as invitations_repo
```

Add the endpoint (next to `login`):

```python
@router.get("/accept-invite")
async def accept_invite(
    token: str | None = None,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Begin invite acceptance: stash the invite token in a short-lived cookie and
    bounce through Google OAuth. The callback consumes the invite on return."""
    if not settings.sso_enabled:
        raise _SSO_DISABLED
    if not token or not token.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing invite token")
    state = oauth.new_state()
    verifier, challenge = oauth.new_pkce()
    url = oauth.build_authorization_url(settings, state=state, code_challenge=challenge)
    resp = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    set_tx_cookie(resp, issue_tx(state, verifier, settings), settings)
    set_invite_cookie(resp, issue_invite(token, settings), settings)
    return resp
```

- [ ] **Step 4: Implement — callback branch + completion helper**

In `callback`, immediately after the verified `email` is established (after the `if not email:` guard, ~line 101) and **before** the allow-list `get_admin_user` lookup, insert:

```python
    invite_cookie = request.cookies.get(INVITE_COOKIE_NAME)
    if invite_cookie:
        return await _complete_invite_accept(
            db, settings, email=email, invite_cookie=invite_cookie
        )
```

Add the completion helpers at module scope:

```python
async def _complete_invite_accept(
    db: AsyncSession,
    settings: Settings,
    *,
    email: str,
    invite_cookie: str,
) -> Response:
    """Consume a pending invite for the Google-verified email and issue a session.

    A valid pending invite is the authorization for a brand-new (non-allow-listed)
    person — the deliberate allow-list bypass. The exact email match against the
    Google-verified identity is the security gate; the token is not a secret.
    """

    def _err(reason: str) -> Response:
        resp = RedirectResponse(
            build_accept_error_url(settings, reason), status_code=status.HTTP_303_SEE_OTHER
        )
        clear_tx_cookie(resp, settings)
        clear_invite_cookie(resp, settings)
        return resp

    async def _audit_denied(invite: Invitation, reason: str) -> None:
        await set_tenant_context(db, invite.organization_id)
        await admin_audit.record(
            db,
            actor_email=email,
            action="invite.accept_denied",
            entity_type="invitation",
            entity_id=invite.email,
            detail={"invite_email": invite.email, "attempted_email": email, "reason": reason},
        )
        await db.commit()

    try:
        claims = decode_invite(invite_cookie, settings)
        token = str(claims["invite_token"])
    except Exception:  # noqa: BLE001 - any bad/tampered invite cookie is "invalid"
        return _err("invalid")

    invite = await invitations_repo.get_by_token(db, token)
    if invite is None:
        logger.warning("invite accept: unknown token")
        return _err("invalid")

    # Email is checked first and unconditionally — never leak invite state to a
    # mismatched identity.
    if invite.email != email:
        await _audit_denied(invite, "mismatch")
        return _err("mismatch")

    # Idempotent re-click: already a member of the target org -> success, no re-consume.
    if await memberships_repo.get_membership(db, email, invite.organization_id) is not None:
        if invite.status is InviteStatus.PENDING:
            await invitations_repo.mark_accepted(db, invite)
            await db.commit()
        user = await admin_users_repo.get_admin_user(db, email)
        return _issue_invite_session(
            settings, email=email, org_id=invite.organization_id, role=invite.role,
            is_super_admin=bool(user and user.is_super_admin),
        )

    now = datetime.now(UTC)
    if invite.status is not InviteStatus.PENDING:
        await _audit_denied(invite, invite.status.value)
        return _err("revoked" if invite.status is InviteStatus.REVOKED else "invalid")
    if invite.expires_at <= now:
        await _audit_denied(invite, "expired")
        return _err("expired")

    # Valid -> consume. add_member ensures the identity (FK target) exists, so a
    # brand-new invitee gets their admin_users row created here.
    await set_tenant_context(db, invite.organization_id)
    user = await admin_users_repo.ensure_identity(db, email=email)
    await memberships_repo.add_member(
        db, email=email, org_id=invite.organization_id, role=invite.role,
        added_by=f"invite:{invite.invited_by}",
    )
    await invitations_repo.mark_accepted(db, invite)
    await admin_audit.record(
        db, actor_email=email, action="invite.accept", entity_type="invitation",
        entity_id=invite.email,
    )
    await admin_audit.record(
        db, actor_email=email, action="auth.login", entity_type="admin_user", entity_id=email
    )
    await admin_users_repo.set_last_active_org(db, email=email, org_id=invite.organization_id)
    await db.commit()
    return _issue_invite_session(
        settings, email=email, org_id=invite.organization_id, role=invite.role,
        is_super_admin=user.is_super_admin,
    )


def _issue_invite_session(
    settings: Settings,
    *,
    email: str,
    org_id: uuid.UUID,
    role: AdminRole,
    is_super_admin: bool,
) -> Response:
    resp = RedirectResponse(
        settings.admin_post_login_redirect, status_code=status.HTTP_303_SEE_OTHER
    )
    set_session_cookie(
        resp,
        issue_session(
            email, active_org_id=org_id, role=role, is_super_admin=is_super_admin,
            acting_as=False, settings=settings,
        ),
        settings,
    )
    clear_tx_cookie(resp, settings)
    clear_invite_cookie(resp, settings)
    return resp
```

- [ ] **Step 5: Run it green**

Run: `cd apps/api && uv run pytest tests/test_invite_accept_flow.py -q`
Expected: PASS (all accept-flow tests).

- [ ] **Step 6: Full backend suite + lint + types**

Run: `cd apps/api && uv run pytest -q && ruff check . && ruff format . && uv run mypy`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/routers/auth.py apps/api/tests/test_invite_accept_flow.py
git commit -m "feat(api): invite-aware OAuth accept flow + brand-new-invitee bypass (P3)"
```

---

## Unit D — Admin UI

### Task D1: Invite types

**Files:**
- Modify: `apps/admin-ui/src/types/api.ts`

- [ ] **Step 1: Add types** (next to the `Member*` types)

```typescript
export type InviteStatus = "pending" | "accepted" | "revoked";

export interface Invite {
  id: string;
  email: string;
  role: AdminUserRole;
  status: InviteStatus;
  accept_url: string;
  expires_at: string;
  created_at: string;
  invited_by: string | null;
}

export interface InviteCreate {
  email: string;
  role: AdminUserRole;
}
```

- [ ] **Step 2: Commit**

```bash
git add apps/admin-ui/src/types/api.ts
git commit -m "feat(admin-ui): invite types (P3)"
```

---

### Task D2: Invite hooks

**Files:**
- Create: `apps/admin-ui/src/features/invites/hooks.ts`
- Test: `apps/admin-ui/src/test/invitesHooks.test.tsx`

- [ ] **Step 1: Write the failing test** (mirror `test/orgsHooks.test.tsx`: mock `../lib/api` + `../components/ui/toast`, import the hook after the mocks, wrap in a `QueryClientProvider` with `retry: false`)

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const postMock = vi.fn();
const getMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
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
const pushToastMock = vi.fn();
vi.mock("../components/ui/toast", () => ({
  pushToast: (m: string, t?: string) => pushToastMock(m, t),
}));

import { useCreateInvite, useInvites, useRevokeInvite } from "../features/invites/hooks";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    Wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  };
}

beforeEach(() => {
  postMock.mockReset();
  getMock.mockReset();
  delMock.mockReset();
  pushToastMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("invite hooks", () => {
  it("useInvites GETs /v1/admin/invites", async () => {
    getMock.mockResolvedValue([]);
    const { Wrapper } = wrapper();
    const { result } = renderHook(() => useInvites(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/v1/admin/invites");
  });

  it("useCreateInvite POSTs and invalidates the invites key", async () => {
    postMock.mockResolvedValue({ id: "1", email: "a@x.com" });
    const { client, Wrapper } = wrapper();
    const spy = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useCreateInvite(), { wrapper: Wrapper });
    result.current.mutate({ email: "a@x.com", role: "viewer" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(postMock).toHaveBeenCalledWith("/v1/admin/invites", { email: "a@x.com", role: "viewer" });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["invites"] });
  });

  it("useRevokeInvite DELETEs by id", async () => {
    delMock.mockResolvedValue(undefined);
    const { Wrapper } = wrapper();
    const { result } = renderHook(() => useRevokeInvite(), { wrapper: Wrapper });
    result.current.mutate("the-id");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(delMock).toHaveBeenCalledWith("/v1/admin/invites/the-id");
  });
});
```

- [ ] **Step 2: Run it red**

Run: `cd apps/admin-ui && npx vitest run src/test/invitesHooks.test.tsx`
Expected: FAIL (hooks module missing).

- [ ] **Step 3: Implement**

```typescript
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { Invite, InviteCreate } from "../../types/api";

// Pending invites for the caller's active org (server-scoped). Org-switch invalidates
// the whole cache (features/orgs/hooks.ts), so a plain key stays correct.
const INVITES_KEY = ["invites"] as const;

export function useInvites() {
  return useQuery<Invite[]>({
    queryKey: INVITES_KEY,
    queryFn: () => api.get<Invite[]>("/v1/admin/invites"),
  });
}

export function useCreateInvite() {
  const qc = useQueryClient();
  return useMutation<Invite, ApiError, InviteCreate>({
    mutationFn: (body) => api.post<Invite>("/v1/admin/invites", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: INVITES_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useRevokeInvite() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/invites/${encodeURIComponent(id)}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: INVITES_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useResendInvite() {
  const qc = useQueryClient();
  return useMutation<Invite, ApiError, string>({
    mutationFn: (id) => api.post<Invite>(`/v1/admin/invites/${encodeURIComponent(id)}/resend`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: INVITES_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}
```

- [ ] **Step 4: Run it green**

Run: `cd apps/admin-ui && npx vitest run src/test/invitesHooks.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/features/invites/hooks.ts apps/admin-ui/src/test/invitesHooks.test.tsx
git commit -m "feat(admin-ui): invite react-query hooks (P3)"
```

---

### Task D3: "Pending invites" section on the Members page

**Files:**
- Create: `apps/admin-ui/src/features/invites/InvitesSection.tsx`
- Modify: `apps/admin-ui/src/features/members/MembersPage.tsx`

- [ ] **Step 1: Implement the section** (reuses the existing `ui/` primitives, `pushToast` for copy confirmation)

```tsx
import { useState, type FormEvent } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { Button } from "../../components/ui/button";
import { pushToast } from "../../components/ui/toast";
import type { AdminUserRole } from "../../types/api";
import { useCreateInvite, useInvites, useResendInvite, useRevokeInvite } from "./hooks";

async function copy(url: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(url);
    pushToast("Invite link copied", "info");
  } catch {
    pushToast("Copy failed — select and copy the link manually");
  }
}

export function InvitesSection() {
  const invites = useInvites();
  const create = useCreateInvite();
  const revoke = useRevokeInvite();
  const resend = useResendInvite();

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<AdminUserRole>("admin");

  function handleInvite(e: FormEvent): void {
    e.preventDefault();
    const trimmed = email.trim().toLowerCase();
    if (trimmed.length === 0) return;
    create.mutate(
      { email: trimmed, role },
      {
        onSuccess: (inv) => {
          setEmail("");
          setRole("admin");
          void copy(inv.accept_url);
        },
      },
    );
  }

  const list = invites.data ?? [];

  return (
    <div className="space-y-3">
      <h2 className="font-display text-lg text-ink-strong">Pending invites</h2>
      <form
        onSubmit={handleInvite}
        className="flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-4 shadow-card"
      >
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="i-email">
            Email
          </label>
          <Input
            id="i-email"
            type="email"
            className="w-72"
            placeholder="person@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="i-role">
            Role
          </label>
          <Select
            id="i-role"
            className="w-40"
            value={role}
            onChange={(e) => setRole(e.target.value as AdminUserRole)}
          >
            <option value="admin">admin</option>
            <option value="viewer">viewer</option>
          </Select>
        </div>
        <Button type="submit" disabled={create.isPending || email.trim().length === 0}>
          {create.isPending ? "Inviting…" : "Invite"}
        </Button>
      </form>

      <Table>
        <Thead>
          <Tr>
            <Th>Email</Th>
            <Th>Role</Th>
            <Th>Invited by</Th>
            <Th>Expires</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </Thead>
        <Tbody>
          {list.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={5}>
                No pending invites.
              </Td>
            </Tr>
          ) : null}
          {list.map((inv) => (
            <Tr key={inv.id}>
              <Td className="font-medium">{inv.email}</Td>
              <Td>{inv.role}</Td>
              <Td className="text-xs text-slate-500">{inv.invited_by ?? "—"}</Td>
              <Td className="text-xs text-slate-500">
                {new Date(inv.expires_at).toLocaleString()}
              </Td>
              <Td className="space-x-2 text-right">
                <Button variant="secondary" onClick={() => void copy(inv.accept_url)}>
                  Copy link
                </Button>
                <Button
                  variant="secondary"
                  disabled={resend.isPending}
                  onClick={() => resend.mutate(inv.id)}
                >
                  Resend
                </Button>
                <Button
                  variant="danger"
                  disabled={revoke.isPending}
                  onClick={() => revoke.mutate(inv.id)}
                >
                  Revoke
                </Button>
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>
    </div>
  );
}
```

> If `Button` has no `variant="secondary"`, use the default variant (omit `variant`) — check `components/ui/button` for the available variants and pick the existing non-destructive one.

- [ ] **Step 2: Mount it on the Members page**

In `apps/admin-ui/src/features/members/MembersPage.tsx`, import `InvitesSection` and render it below the members table (inside the top-level `<div className="space-y-4">`, after the `<ConfirmDialog>`):

```tsx
import { InvitesSection } from "../invites/InvitesSection";
```

```tsx
      <InvitesSection />
```

- [ ] **Step 3: Verify build/lint/types**

Run: `cd apps/admin-ui && npm run typecheck && npm run lint`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add apps/admin-ui/src/features/invites/InvitesSection.tsx apps/admin-ui/src/features/members/MembersPage.tsx
git commit -m "feat(admin-ui): pending-invites section on Members page (P3)"
```

---

### Task D4: Public `/accept-invite` route

**Files:**
- Create: `apps/admin-ui/src/features/invites/AcceptInvitePage.tsx`
- Modify: `apps/admin-ui/src/routes.tsx`

The route MUST be a **top-level sibling** of the `"/"` route object so it renders WITHOUT `RequireAuth`/`AppLayout` (no session probe, no login redirect).

- [ ] **Step 1: Implement the page**

```tsx
import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

const REASONS: Record<string, string> = {
  mismatch: "This invitation was issued to a different email address. Sign in with the address it was sent to.",
  expired: "This invitation has expired. Ask an organization admin to send a new one.",
  revoked: "This invitation has been revoked.",
  invalid: "This invitation link is not valid.",
};

// Public, unauthenticated. With ?token it forwards into the API accept endpoint
// (which bounces through Google). With ?status=error it shows a friendly message.
export function AcceptInvitePage() {
  const [params] = useSearchParams();
  const status = params.get("status");
  const token = params.get("token");

  useEffect(() => {
    if (status !== "error" && token) {
      window.location.assign(`/v1/auth/accept-invite?token=${encodeURIComponent(token)}`);
    }
  }, [status, token]);

  if (status === "error") {
    const reason = params.get("reason") ?? "invalid";
    return (
      <div className="flex h-screen items-center justify-center p-6">
        <div className="max-w-md text-center text-slate-700">
          <p className="font-medium">Can't accept this invitation</p>
          <p className="mt-1 text-sm text-slate-500">{REASONS[reason] ?? REASONS.invalid}</p>
          <a className="mt-4 inline-block text-sm text-blue-700 underline" href="/v1/auth/login">
            Go to sign in
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen items-center justify-center">
      <span className="text-slate-600">Redirecting to sign in…</span>
    </div>
  );
}
```

- [ ] **Step 2: Register the route**

In `apps/admin-ui/src/routes.tsx`, import the page and add a sibling entry to the `createBrowserRouter([...])` array (a peer of the `{ path: "/", ... }` object, NOT inside its `children`):

```tsx
import { AcceptInvitePage } from "./features/invites/AcceptInvitePage";
```

```tsx
  { path: "/accept-invite", element: <AcceptInvitePage /> },
```

- [ ] **Step 3: Verify build/lint/types + full UI suite**

Run: `cd apps/admin-ui && npm run typecheck && npm run lint && npm test`
Expected: clean; tests pass (re-run any 5000ms-timeout flakes in isolation per the known admin-ui flakiness).

- [ ] **Step 4: Commit**

```bash
git add apps/admin-ui/src/features/invites/AcceptInvitePage.tsx apps/admin-ui/src/routes.tsx
git commit -m "feat(admin-ui): public /accept-invite route (P3)"
```

---

## Unit E — Cross-org isolation & RBAC proof (under `usan_app`)

### Task E1: Invitation isolation suite

**Files:**
- Create: `apps/api/tests/test_invitations_isolation.py`

This is the bulletproof-isolation proof: it runs endpoints under the non-superuser `usan_app` role with the production org seam (`get_tenant_db` scoped to `principal.active_org_id`), proving an org cannot see or accept another org's invites. It mirrors the `isolation_client` fixture + `_member_cookie`/`_act_as_cookie` helpers from `test_rls_p2_isolation.py` (define a local copy of the fixture/helpers in this module).

- [ ] **Step 1: Write the tests**

```python
import asyncio

import pytest
from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.auth import AdminPrincipal, get_tenant_db, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context


def _member_cookie(email, org_id, role):
    return {
        SESSION_COOKIE_NAME: issue_session(
            email, active_org_id=org_id, role=role, is_super_admin=False, acting_as=False,
            settings=get_settings(),
        )
    }


def _act_as_cookie(email, org_id):
    return {
        SESSION_COOKIE_NAME: issue_session(
            email, active_org_id=org_id, role=AdminRole.ADMIN, is_super_admin=True, acting_as=True,
            settings=get_settings(),
        )
    }


def _super_url(app_async_database_url: str) -> str:
    return app_async_database_url.replace("usan_app:usan_app@", "usan:usan@", 1)


async def _seed_member(super_url, email, org_id, role):
    eng = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                    "VALUES (:e, false, 'active', 'test') ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email},
            )
            await conn.execute(
                text(
                    "INSERT INTO memberships (email, organization_id, role, added_by) "
                    "VALUES (:e, :o, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email, organization_id) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email, "o": org_id, "r": role},
            )
    finally:
        await eng.dispose()


@pytest.fixture
def isolation_client(client, app_async_database_url):
    tenant_engine = create_async_engine(app_async_database_url, poolclass=NullPool)
    tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)

    async def _tenant_override(principal: AdminPrincipal = Depends(require_admin_session)):
        if principal.active_org_id is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "select an organization first")
        async with tenant_factory() as session:
            await set_tenant_context(session, principal.active_org_id)
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    client.app.dependency_overrides[get_tenant_db] = _tenant_override
    try:
        yield client, _super_url(app_async_database_url)
    finally:
        client.app.dependency_overrides.pop(get_tenant_db, None)
        asyncio.run(tenant_engine.dispose())


def test_invites_isolated_between_orgs(isolation_client, two_orgs):
    client, super_url = isolation_client
    org_a, org_b = two_orgs
    asyncio.run(_seed_member(super_url, "a-admin@x.com", org_a, "admin"))
    asyncio.run(_seed_member(super_url, "b-admin@x.com", org_b, "admin"))

    created = client.post(
        "/v1/admin/invites", json={"email": "guest@x.com", "role": "viewer"},
        cookies=_member_cookie("a-admin@x.com", org_a, AdminRole.ADMIN),
    )
    assert created.status_code == 201
    iid = created.json()["id"]

    in_b = client.get("/v1/admin/invites", cookies=_member_cookie("b-admin@x.com", org_b, AdminRole.ADMIN))
    assert in_b.status_code == 200
    assert in_b.json() == []

    rev_b = client.delete(
        f"/v1/admin/invites/{iid}", cookies=_member_cookie("b-admin@x.com", org_b, AdminRole.ADMIN)
    )
    assert rev_b.status_code == 404

    in_a = client.get("/v1/admin/invites", cookies=_member_cookie("a-admin@x.com", org_a, AdminRole.ADMIN))
    assert [i["email"] for i in in_a.json()] == ["guest@x.com"]


def test_super_admin_act_as_can_invite(isolation_client, two_orgs):
    client, _ = isolation_client
    org_a, _ = two_orgs
    r = client.post(
        "/v1/admin/invites", json={"email": "viaact@x.com", "role": "viewer"},
        cookies=_act_as_cookie("super@x.com", org_a),
    )
    assert r.status_code == 201


def test_no_active_org_409(isolation_client):
    client, _ = isolation_client
    token = issue_session(
        "super@x.com", active_org_id=None, role=None, is_super_admin=True, acting_as=False,
        settings=get_settings(),
    )
    r = client.get("/v1/admin/invites", cookies={SESSION_COOKIE_NAME: token})
    assert r.status_code == 409
```

- [ ] **Step 2: Run it**

Run: `cd apps/api && uv run pytest tests/test_invitations_isolation.py -q`
Expected: PASS.

- [ ] **Step 3: Full suite + lint + types + commit**

```bash
cd apps/api && uv run pytest -q && ruff check . && ruff format . && uv run mypy
git add apps/api/tests/test_invitations_isolation.py
git commit -m "test(api): cross-org invitation isolation under usan_app (P3)"
```

---

## Final review (after all units)

- [ ] Dispatch a final code review over the whole branch diff (`git diff main...HEAD`): correctness of the accept flow's allow-list bypass (exact email match, single-use, no consume on failure), org-scoping of every `invitations` query, audit coverage (`invite.create`/`accept`/`accept_denied`/`revoke`/`resend`), and admin-ui contract alignment.
- [ ] Run the full backend + UI suites once more; confirm `ruff`, `mypy`, `npm run lint`, `npm run typecheck` are all green.
- [ ] Use superpowers:finishing-a-development-branch to open the PR.

## Self-review notes (plan author)

- **Spec coverage:** invitations table + repo (A), management API (B), accept flow incl. brand-new bypass (C), UI section + public route (D), isolation under `usan_app` (E) — all spec units mapped. Copyable-link delivery: `accept_url` returned by B and surfaced by D3; no email provider. Lazy expiry: `is_usable` + accept-time check, no cron.
- **Type consistency:** `InviteStatus`/`AdminRole` enums; `Invitation` columns match the migration; `InviteOut` fields match `_out`; UI `Invite` type matches `InviteOut`; repo error types (`AlreadyMemberError`/`NotPendingError`) caught in B's handlers.
- **Known-pattern references (not placeholders):** C2 reuses the `sso_client` + oauth-monkeypatch fixtures from `test_auth_flow_p2.py`, and A4/C1 reuse the existing minimal-`Settings` test construction — the implementer reads those files to copy the exact harness rather than re-inventing it.
