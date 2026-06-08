# Admin UI — P1 Backend Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the database + API foundation for admin-managed agent configuration profiles (create/clone, edit draft, publish with immutable versions, rollback, set per-direction defaults), guarded temporarily by the operator token until SSO lands in P3.

**Architecture:** New Postgres tables (`agent_profiles`, `agent_profile_versions`, `admin_users`, `admin_audit_log`) plus FK columns on `elders`/`calls`, all owned by `apps/api`. A validated Pydantic `AgentConfig` document (all knob bundles) is stored as JSONB in a profile's mutable `draft_config` and frozen into immutable version snapshots on publish. A repository layer owns the publish/version/rollback/default-uniqueness logic; a thin FastAPI router exposes it. No agent integration and no UI in this phase (those are P2 and P4).

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (`Mapped`/`mapped_column`), Alembic (hand-written raw SQL migrations), Pydantic v2, asyncpg/Postgres, pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-06-07-admin-ui-design.md`

---

## Conventions (read before starting)

These are the **exact** house patterns verified against the codebase. Deviating breaks CI (`ruff check`, `uv run mypy`) or the testcontainer migration run.

- **Migrations** are hand-written raw SQL via `op.execute("...")` — never `op.create_table`/`sa.*`; `sqlalchemy` is not imported. Enums are `CREATE TYPE` *before* the table; `downgrade()` is the exact reverse with `IF EXISTS`/`CASCADE`. The current head revision is **`0009`** (`migrations/versions/0009_grafana_ro_role.py`), so this migration is **`0010`** with `down_revision="0009"`.
- **Models** use SQLAlchemy 2.0 `Mapped[...]` + `mapped_column(...)`. PG enums map with `SAEnum(EnumCls, name="<pg_type>", values_callable=_enum_values, create_type=False)` — `_enum_values` is defined in `db/models.py:26`. There is **no `relationship()`** anywhere — models are FK-only. The Python attr for a `metadata` column must be `meta` (Declarative reserves `metadata`).
- **Repositories** are module-level `async def` functions in `repositories/<x>.py`; first param is always `db: AsyncSession`; they `db.add`/`db.flush`/`db.refresh` and mutate ORM objects in place but **never commit** — the router owns `commit`/`rollback`. Mutating-arg functions force keyword-only args with a bare `*`.
- **Schemas** are plain `class X(BaseModel)` (no `ConfigDict`/`from_attributes`); ORM→response is an explicit `@classmethod from_model(...)`. `*Update` schemas make every field `Optional` default `None`.
- **Routers**: `router = APIRouter(prefix="/v1/...", tags=[...])`; collection root is `""`; success codes use `status.HTTP_*` in the decorator, errors raise `HTTPException(status_code=<bare int>, detail="lowercase msg")`. Session dep: `from usan_api.db.session import get_db`. Operator auth: `from usan_api.auth import require_operator_token`.
- **Tests**: real Postgres testcontainer; schema built by `alembic upgrade head` (so the migration MUST apply); `asyncio_mode="auto"` (no marker needed). `client` fixture overrides `get_db`; `operator_headers` fixture gives the bearer header. The teardown TRUNCATEs **only `calls, dnc_list, elders`** — new tables must be added there.
- **Commands** (run from `apps/api/`): `uv run pytest -v`, `ruff check . && ruff format .`, `uv run mypy`. Commit format: `type(scope): description`, scope `api`.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `apps/api/src/usan_api/db/base.py` | `ProfileStatus`, `AdminRole` enums | Modify |
| `apps/api/src/usan_api/db/models.py` | 4 new models + `Elder`/`Call` FK columns | Modify |
| `apps/api/migrations/versions/0010_admin_agent_profiles.py` | schema migration | Create |
| `apps/api/src/usan_api/schemas/agent_config.py` | `AgentConfig` document + validators + `DEFAULT_AGENT_CONFIG` | Create |
| `apps/api/src/usan_api/schemas/agent_profile.py` | profile request/response schemas | Create |
| `apps/api/src/usan_api/repositories/admin_audit.py` | append-only audit writer/reader | Create |
| `apps/api/src/usan_api/repositories/agent_profiles.py` | profile CRUD + publish/version/rollback/defaults/archive | Create |
| `apps/api/src/usan_api/admin_actor.py` | `get_actor_email` seam (P1 stub → P3 SSO) | Create |
| `apps/api/src/usan_api/routers/admin_profiles.py` | admin profile endpoints | Create |
| `apps/api/src/usan_api/main.py` | register the router | Modify |
| `apps/api/tests/conftest.py` | add new tables to TRUNCATE | Modify |
| `apps/api/tests/test_agent_config_schema.py` | config schema unit tests | Create |
| `apps/api/tests/test_agent_profiles_repo.py` | repository unit tests | Create |
| `apps/api/tests/test_admin_profiles_api.py` | router integration tests | Create |

---

## Task 1: Add enums to `db/base.py`

**Files:**
- Modify: `apps/api/src/usan_api/db/base.py`

- [ ] **Step 1: Add the two enums**

Append to `apps/api/src/usan_api/db/base.py` (after `CallStatus`):

```python
class ProfileStatus(enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AdminRole(enum.Enum):
    ADMIN = "admin"
    VIEWER = "viewer"
```

- [ ] **Step 2: Verify it imports**

Run: `cd apps/api && uv run python -c "from usan_api.db.base import ProfileStatus, AdminRole; print(ProfileStatus.ACTIVE.value, AdminRole.ADMIN.value)"`
Expected: `active admin`

- [ ] **Step 3: Commit**

```bash
git add apps/api/src/usan_api/db/base.py
git commit -m "feat(api): add ProfileStatus and AdminRole enums"
```

---

## Task 2: Add ORM models + FK columns

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py`

- [ ] **Step 1: Extend the base import**

Change the existing import line in `db/models.py`:

```python
from usan_api.db.base import Base, CallDirection, CallStatus
```

to:

```python
from usan_api.db.base import AdminRole, Base, CallDirection, CallStatus, ProfileStatus
```

- [ ] **Step 2: Add the `agent_profile_id` column to `Elder`**

In the `Elder` model, immediately after the `preferred_voice` line, add:

```python
    agent_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL")
    )
```

- [ ] **Step 3: Add the `profile_override` column to `Call`**

In the `Call` model, immediately after the `dynamic_vars` block, add:

```python
    profile_override: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_profiles.id", ondelete="SET NULL")
    )
```

- [ ] **Step 4: Add the four new models**

Append to the end of `db/models.py`:

```python
class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProfileStatus] = mapped_column(
        SAEnum(
            ProfileStatus,
            name="profile_status",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        server_default=ProfileStatus.ACTIVE.value,
    )
    draft_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    # The live version number (joins agent_profile_versions on (id, version));
    # NULL means the profile has never been published.
    published_version: Mapped[int | None] = mapped_column(Integer)
    is_default_outbound: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_default_inbound: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_by: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AgentProfileVersion(Base):
    __tablename__ = "agent_profile_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    note: Mapped[str | None] = mapped_column(Text)
    published_by: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminUser(Base):
    __tablename__ = "admin_users"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    role: Mapped[AdminRole] = mapped_column(
        SAEnum(
            AdminRole,
            name="admin_role",
            values_callable=_enum_values,
            create_type=False,
        ),
        nullable=False,
        server_default=AdminRole.ADMIN.value,
    )
    added_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_email: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 5: Verify models import cleanly**

Run: `cd apps/api && uv run python -c "from usan_api.db import models; print([m for m in dir(models) if 'Agent' in m or 'Admin' in m])"`
Expected: a list containing `AgentProfile`, `AgentProfileVersion`, `AdminAuditLog`, `AdminUser`

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/db/models.py
git commit -m "feat(api): add agent-profile + admin ORM models and FK columns"
```

---

## Task 3: Migration `0010`

**Files:**
- Create: `apps/api/migrations/versions/0010_admin_agent_profiles.py`

- [ ] **Step 1: Write the migration**

Create `apps/api/migrations/versions/0010_admin_agent_profiles.py`:

```python
"""admin + agent-profile tables for the admin UI

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-07

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. enums first (before any table that references them)
    op.execute("CREATE TYPE profile_status AS ENUM ('active', 'archived')")
    op.execute("CREATE TYPE admin_role AS ENUM ('admin', 'viewer')")

    # 2. agent_profiles (parent; no FK to versions — the live version is tracked
    #    by the integer `published_version`, joined to agent_profile_versions on
    #    (id, version), which avoids a circular FK).
    op.execute(
        """
        CREATE TABLE agent_profiles (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name                TEXT NOT NULL UNIQUE,
            description         TEXT,
            status              profile_status NOT NULL DEFAULT 'active',
            draft_config        JSONB NOT NULL DEFAULT '{}',
            published_version   INTEGER,
            is_default_outbound BOOLEAN NOT NULL DEFAULT false,
            is_default_inbound  BOOLEAN NOT NULL DEFAULT false,
            created_by          TEXT,
            updated_by          TEXT,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # 3. agent_profile_versions (immutable snapshots; child of agent_profiles)
    op.execute(
        """
        CREATE TABLE agent_profile_versions (
            id           BIGSERIAL PRIMARY KEY,
            profile_id   UUID NOT NULL REFERENCES agent_profiles(id) ON DELETE CASCADE,
            version      INTEGER NOT NULL,
            config       JSONB NOT NULL DEFAULT '{}',
            note         TEXT,
            published_by TEXT,
            published_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # 4. admin_users (email PK; allow-list seeded in P3)
    op.execute(
        """
        CREATE TABLE admin_users (
            email      TEXT PRIMARY KEY,
            role       admin_role NOT NULL DEFAULT 'admin',
            added_by   TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # 5. admin_audit_log (append-only)
    op.execute(
        """
        CREATE TABLE admin_audit_log (
            id          BIGSERIAL PRIMARY KEY,
            actor_email TEXT NOT NULL,
            action      TEXT NOT NULL,
            entity_type TEXT,
            entity_id   TEXT,
            detail      JSONB NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    # 6. FK columns on existing tables
    op.execute(
        "ALTER TABLE elders ADD COLUMN agent_profile_id UUID "
        "REFERENCES agent_profiles(id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE calls ADD COLUMN profile_override UUID "
        "REFERENCES agent_profiles(id) ON DELETE SET NULL"
    )

    # 7. indexes
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_profile_versions_profile_version "
        "ON agent_profile_versions(profile_id, version)"
    )
    # At most one default profile per direction (partial-unique on the value `true`).
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_profiles_default_outbound "
        "ON agent_profiles((is_default_outbound)) WHERE is_default_outbound"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_profiles_default_inbound "
        "ON agent_profiles((is_default_inbound)) WHERE is_default_inbound"
    )
    op.execute(
        "CREATE INDEX idx_admin_audit_log_created ON admin_audit_log(created_at DESC)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS profile_override")
    op.execute("ALTER TABLE elders DROP COLUMN IF EXISTS agent_profile_id")
    op.execute("DROP TABLE IF EXISTS admin_audit_log CASCADE")
    op.execute("DROP TABLE IF EXISTS admin_users CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_profile_versions CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_profiles CASCADE")
    op.execute("DROP TYPE IF EXISTS admin_role")
    op.execute("DROP TYPE IF EXISTS profile_status")
```

- [ ] **Step 2: Apply and roll back against a scratch DB to prove both directions**

Start a throwaway Postgres and run the full chain (this mirrors what the test container does):

```bash
cd apps/api
docker run -d --rm --name usan_mig_check -e POSTGRES_USER=usan -e POSTGRES_PASSWORD=usan -e POSTGRES_DB=usan -p 55432:5432 pgvector/pgvector:pg18
sleep 4
DATABASE_URL="postgresql://usan:usan@127.0.0.1:55432/usan" LIVEKIT_API_KEY=k LIVEKIT_API_SECRET=$(python -c "print('s'*32)") LIVEKIT_URL=ws://x:7880 JWT_SIGNING_KEY=$(python -c "print('s'*32)") OPERATOR_API_KEY=$(python -c "print('o'*32)") uv run alembic upgrade head
DATABASE_URL="postgresql://usan:usan@127.0.0.1:55432/usan" LIVEKIT_API_KEY=k LIVEKIT_API_SECRET=$(python -c "print('s'*32)") LIVEKIT_URL=ws://x:7880 JWT_SIGNING_KEY=$(python -c "print('s'*32)") OPERATOR_API_KEY=$(python -c "print('o'*32)") uv run alembic downgrade -1
docker stop usan_mig_check
```

Expected: `upgrade` runs to `0010` with no error; `downgrade` removes `0010` cleanly with no error.

- [ ] **Step 3: Commit**

```bash
git add apps/api/migrations/versions/0010_admin_agent_profiles.py
git commit -m "feat(api): migration 0010 — admin + agent-profile tables"
```

---

## Task 4: Add new tables to the test TRUNCATE list

**Files:**
- Modify: `apps/api/tests/conftest.py:58`

- [ ] **Step 1: Extend the TRUNCATE statement**

Replace this line in `_truncate_and_dispose`:

```python
            await conn.execute(text("TRUNCATE calls, dnc_list, elders RESTART IDENTITY CASCADE"))
```

with:

```python
            await conn.execute(
                text(
                    "TRUNCATE agent_profile_versions, agent_profiles, admin_audit_log, "
                    "admin_users, calls, dnc_list, elders RESTART IDENTITY CASCADE"
                )
            )
```

- [ ] **Step 2: Commit**

```bash
git add apps/api/tests/conftest.py
git commit -m "test(api): truncate agent-profile + admin tables between client tests"
```

---

## Task 5: The `AgentConfig` document schema

**Files:**
- Create: `apps/api/src/usan_api/schemas/agent_config.py`
- Test: `apps/api/tests/test_agent_config_schema.py`

This is the validated config document stored in `draft_config` and frozen into versions. Defaults reproduce today's hardcoded agent constants so a freshly-created profile behaves like the current system.

- [ ] **Step 1: Write the failing test for defaults + round-trip**

Create `apps/api/tests/test_agent_config_schema.py`:

```python
import pytest
from pydantic import ValidationError

from usan_api.schemas.agent_config import (
    DEFAULT_AGENT_CONFIG,
    AgentConfig,
    PromptsConfig,
    ToolsConfig,
)


def test_default_config_matches_current_agent_constants():
    cfg = DEFAULT_AGENT_CONFIG
    assert cfg.llm.model == "gemini-3.1-flash-lite"
    assert cfg.stt.model == "ink-whisper"
    assert cfg.timing.answer_timeout_s == 50.0
    assert cfg.timing.max_call_duration_s == 1800
    assert set(cfg.tools.enabled) == {
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "end_call",
    }
    assert cfg.prompts.greeting.startswith("Hello! This is your daily check-in")


def test_config_round_trips_through_dict():
    data = DEFAULT_AGENT_CONFIG.model_dump()
    restored = AgentConfig.model_validate(data)
    assert restored == DEFAULT_AGENT_CONFIG
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_agent_config_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.schemas.agent_config'`

- [ ] **Step 3: Write the schema**

Create `apps/api/src/usan_api/schemas/agent_config.py`:

```python
"""The admin-editable agent configuration document (design spec Appendix A).

Stored as JSONB in ``agent_profiles.draft_config`` and frozen into
``agent_profile_versions.config`` on publish. Validated here so the JSONB is
structured, not free-form. Defaults reproduce the agent's current hardcoded
constants (services/agent: pipeline.py, check_in.py, worker.py) so a new profile
behaves like today's system. ``None`` on an optional knob means "use the agent
plugin default".
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Tool names the agent can register; mirrors check_in.build_check_in_agent().
TOOL_NAMES = frozenset({"log_wellness", "log_medication", "get_today_meds", "end_call"})
# Personalization slots allowed in the inbound template (check_in.py rendering).
ALLOWED_TEMPLATE_SLOTS = frozenset({"elder_name", "last_check_in_line"})

_SLOT_RE = re.compile(r"\{([^{}]*)\}")


def _reject_braces(value: str) -> str:
    """Reject raw format-slot braces: they break str.format and are an injection
    vector (cf. check_in._PROMPT_UNSAFE). Used on every prompt field except the
    one explicit personalization template."""
    if "{" in value or "}" in value:
        raise ValueError("must not contain '{' or '}'")
    return value


class PromptsConfig(BaseModel):
    system_prompt: str = Field(max_length=4000)
    greeting: str = Field(max_length=1000)
    recording_disclosure: str = Field(max_length=1000)
    voicemail_message: str = Field(max_length=1000)
    checkin_flow_instructions: str = Field(max_length=6000)
    goodbye_message: str = Field(max_length=1000)
    inbound_opening: str = Field(max_length=1000)
    inbound_personalization_template: str = Field(max_length=6000)

    @field_validator(
        "system_prompt",
        "greeting",
        "recording_disclosure",
        "voicemail_message",
        "checkin_flow_instructions",
        "goodbye_message",
        "inbound_opening",
    )
    @classmethod
    def _no_braces(cls, v: str) -> str:
        return _reject_braces(v)

    @field_validator("inbound_personalization_template")
    @classmethod
    def _only_allowed_slots(cls, v: str) -> str:
        slots = _SLOT_RE.findall(v)
        bad = [s for s in slots if s not in ALLOWED_TEMPLATE_SLOTS]
        if bad:
            raise ValueError(
                f"unknown template slot(s): {', '.join(sorted(set(bad)))}; "
                f"allowed: {', '.join(sorted(ALLOWED_TEMPLATE_SLOTS))}"
            )
        # Reject stray braces not part of a recognized slot.
        stripped = _SLOT_RE.sub("", v)
        if "{" in stripped or "}" in stripped:
            raise ValueError("contains an unmatched '{' or '}'")
        return v


class VoiceConfig(BaseModel):
    cartesia_voice_id: str | None = Field(default=None, max_length=200)
    tts_model: str | None = Field(default=None, max_length=100)
    speed: float | None = Field(default=None, ge=0.25, le=4.0)
    language: str | None = Field(default=None, max_length=20)


class LLMConfig(BaseModel):
    model: str = Field(default="gemini-3.1-flash-lite", min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class STTConfig(BaseModel):
    model: str = Field(default="ink-whisper", min_length=1, max_length=200)
    language: str | None = Field(default=None, max_length=20)


class TimingConfig(BaseModel):
    answer_timeout_s: float = Field(default=50.0, ge=5.0, le=180.0)
    max_call_duration_s: int = Field(default=1800, ge=60, le=7200)


class ToolsConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "log_wellness",
            "log_medication",
            "get_today_meds",
            "end_call",
        ]
    )

    @field_validator("enabled")
    @classmethod
    def _known_tools(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in TOOL_NAMES]
        if bad:
            raise ValueError(f"unknown tool(s): {', '.join(sorted(set(bad)))}")
        return v


class VoicemailDetectionConfig(BaseModel):
    window_s: float = Field(default=3.0, ge=0.5, le=30.0)
    # Empty list means "use the agent's built-in detection patterns".
    trigger_phrases: list[str] = Field(default_factory=list)


class SpeechAdvancedConfig(BaseModel):
    # None on each → use the LiveKit plugin default (silero VAD / EnglishModel etc).
    vad_min_silence_s: float | None = Field(default=None, ge=0.0, le=5.0)
    vad_activation_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    turn_detection: Literal["english", "multilingual", "vad"] | None = None
    min_endpointing_delay_s: float | None = Field(default=None, ge=0.0, le=10.0)
    max_endpointing_delay_s: float | None = Field(default=None, ge=0.0, le=30.0)
    min_interruption_duration_s: float | None = Field(default=None, ge=0.0, le=5.0)
    min_interruption_words: int | None = Field(default=None, ge=0, le=20)


class AgentConfig(BaseModel):
    prompts: PromptsConfig
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    voicemail_detection: VoicemailDetectionConfig = Field(
        default_factory=VoicemailDetectionConfig
    )
    speech_advanced: SpeechAdvancedConfig = Field(default_factory=SpeechAdvancedConfig)


# Defaults below are copied verbatim from the agent's current constants so a new
# profile reproduces today's behavior. Keep in sync if those constants change.
DEFAULT_AGENT_CONFIG = AgentConfig(
    prompts=PromptsConfig(
        system_prompt=(
            "You are a warm, patient daily check-in assistant from USAN Retirement.\n"
            "You are speaking to an elder over the phone. Speak slowly, clearly, and kindly.\n"
            "Keep responses short — one or two sentences. Pause to let them respond.\n"
        ),
        greeting=(
            "Hello! This is your daily check-in from USAN. How are you feeling today?"
        ),
        recording_disclosure=(
            "Before we begin, please know that this call is recorded for quality and "
            "to support your care."
        ),
        voicemail_message=(
            "Hello, this is your daily check-in from USAN Retirement. "
            "We're sorry we missed you. We'll try again a little later. "
            "Take care, and have a wonderful day."
        ),
        checkin_flow_instructions=(
            "You are a warm, patient daily check-in caller from USAN Retirement,\n"
            "speaking to an elder on the phone. Speak slowly and kindly, one or two "
            "short sentences at a time,\nand pause for them to answer.\n\n"
            "Conduct the check-in in this order, adapting naturally to their answers:\n"
            "1. Ask how they are feeling today and roughly how their mood is. Record it "
            "with `log_wellness`\n   (mood 1-5 where 5 is great; include any pain level "
            "0-10 and a short note if they mention it).\n"
            "2. Use `get_today_meds` to find out which medications they take today, then "
            "gently ask whether\n   they have taken each one. Record each with "
            "`log_medication`.\n"
            "3. When the check-in is complete, thank them and call `end_call` with a "
            'short reason\n   (for example "check_in_complete").\n\n'
            "Never read out internal IDs or tool names. If a tool reports a problem, "
            "reassure them calmly and\ncontinue — do not repeat a failed action more "
            "than once.\n"
        ),
        goodbye_message=(
            "Thank you for your time today. Take care, and have a wonderful day. Goodbye."
        ),
        inbound_opening=(
            "Greet the caller warmly by name if you know it, and ask how they are "
            "feeling today to begin the daily check-in."
        ),
        inbound_personalization_template=(
            "You are a warm, patient check-in assistant from USAN Retirement,\n"
            "speaking with {elder_name}, who has just called in. Speak slowly and "
            "kindly, one or two short\nsentences at a time, and pause for them to "
            "answer.\n{last_check_in_line}\n"
            "Conduct the check-in in this order, adapting naturally to their answers:\n"
            "1. Greet {elder_name} warmly by name, then ask how they are feeling today "
            "and roughly how their\n   mood is. Record it with `log_wellness` (mood 1-5 "
            "where 5 is great; include any pain level 0-10\n   and a short note if they "
            "mention it).\n"
            "2. Use `get_today_meds` to find out which medications they take today, then "
            "gently ask whether\n   they have taken each one. Record each with "
            "`log_medication`.\n"
            "3. When the check-in is complete, thank them and call `end_call` with a "
            'short reason\n   (for example "check_in_complete").\n\n'
            "Never read out internal IDs or tool names. If a tool reports a problem, "
            "reassure them calmly and\ncontinue — do not repeat a failed action more "
            "than once.\n"
        ),
    ),
)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_agent_config_schema.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Add validation tests**

Append to `apps/api/tests/test_agent_config_schema.py`:

```python
def test_prompt_field_rejects_braces():
    bad = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    bad["greeting"] = "Hello {name}"
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(bad)


def test_personalization_template_rejects_unknown_slot():
    bad = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    bad["inbound_personalization_template"] = "Hi {ssn}"
    with pytest.raises(ValidationError):
        PromptsConfig.model_validate(bad)


def test_personalization_template_accepts_allowed_slots():
    ok = DEFAULT_AGENT_CONFIG.prompts.model_dump()
    ok["inbound_personalization_template"] = "Hi {elder_name}. {last_check_in_line}"
    assert PromptsConfig.model_validate(ok)


def test_tools_rejects_unknown_tool():
    with pytest.raises(ValidationError):
        ToolsConfig(enabled=["log_wellness", "launch_missiles"])
```

- [ ] **Step 6: Run the full schema test file**

Run: `cd apps/api && uv run pytest tests/test_agent_config_schema.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/schemas/agent_config.py apps/api/tests/test_agent_config_schema.py
git commit -m "feat(api): AgentConfig document schema with prompt-safety validation"
```

---

## Task 6: Profile request/response schemas

**Files:**
- Create: `apps/api/src/usan_api/schemas/agent_profile.py`

- [ ] **Step 1: Write the schemas**

Create `apps/api/src/usan_api/schemas/agent_profile.py`:

```python
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, AgentProfileVersion
from usan_api.schemas.agent_config import AgentConfig

NAME_MAX_LENGTH = 120
DESCRIPTION_MAX_LENGTH = 1000
NOTE_MAX_LENGTH = 500


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    clone_from: uuid.UUID | None = None


class DraftUpdate(BaseModel):
    config: AgentConfig
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)


class PublishRequest(BaseModel):
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)


class SetDefaultRequest(BaseModel):
    direction: Literal["inbound", "outbound"]


class ProfileSummary(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: ProfileStatus
    is_default_inbound: bool
    is_default_outbound: bool
    published_version: int | None
    has_unpublished_draft: bool
    assigned_elder_count: int
    updated_at: datetime

    @classmethod
    def from_model(
        cls,
        profile: AgentProfile,
        *,
        has_unpublished_draft: bool,
        assigned_elder_count: int,
    ) -> "ProfileSummary":
        return cls(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            status=profile.status,
            is_default_inbound=profile.is_default_inbound,
            is_default_outbound=profile.is_default_outbound,
            published_version=profile.published_version,
            has_unpublished_draft=has_unpublished_draft,
            assigned_elder_count=assigned_elder_count,
            updated_at=profile.updated_at,
        )


class ProfileDetail(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: ProfileStatus
    is_default_inbound: bool
    is_default_outbound: bool
    published_version: int | None
    draft_config: AgentConfig
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, profile: AgentProfile) -> "ProfileDetail":
        return cls(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            status=profile.status,
            is_default_inbound=profile.is_default_inbound,
            is_default_outbound=profile.is_default_outbound,
            published_version=profile.published_version,
            draft_config=AgentConfig.model_validate(profile.draft_config),
            created_by=profile.created_by,
            updated_by=profile.updated_by,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )


class VersionSummary(BaseModel):
    version: int
    note: str | None
    published_by: str | None
    published_at: datetime

    @classmethod
    def from_model(cls, version: AgentProfileVersion) -> "VersionSummary":
        return cls(
            version=version.version,
            note=version.note,
            published_by=version.published_by,
            published_at=version.published_at,
        )


class VersionDetail(VersionSummary):
    config: AgentConfig

    @classmethod
    def from_model(cls, version: AgentProfileVersion) -> "VersionDetail":
        return cls(
            version=version.version,
            note=version.note,
            published_by=version.published_by,
            published_at=version.published_at,
            config=AgentConfig.model_validate(version.config),
        )
```

- [ ] **Step 2: Verify it imports**

Run: `cd apps/api && uv run python -c "from usan_api.schemas.agent_profile import ProfileCreate, ProfileDetail, VersionDetail; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add apps/api/src/usan_api/schemas/agent_profile.py
git commit -m "feat(api): agent-profile request/response schemas"
```

---

## Task 7: Audit repository

**Files:**
- Create: `apps/api/src/usan_api/repositories/admin_audit.py`

- [ ] **Step 1: Write the repository**

Create `apps/api/src/usan_api/repositories/admin_audit.py`:

```python
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import AdminAuditLog


async def record(
    db: AsyncSession,
    *,
    actor_email: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AdminAuditLog:
    """Append an audit entry. Caller owns the surrounding transaction/commit."""
    entry = AdminAuditLog(
        actor_email=actor_email,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail or {},
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry)
    return entry


async def list_recent(db: AsyncSession, *, limit: int = 100) -> list[AdminAuditLog]:
    result = await db.execute(
        select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
```

- [ ] **Step 2: Verify it imports**

Run: `cd apps/api && uv run python -c "from usan_api.repositories import admin_audit; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add apps/api/src/usan_api/repositories/admin_audit.py
git commit -m "feat(api): admin audit-log repository"
```

---

## Task 8: Profile repository

**Files:**
- Create: `apps/api/src/usan_api/repositories/agent_profiles.py`
- Test: `apps/api/tests/test_agent_profiles_repo.py`

- [ ] **Step 1: Write the failing repo test**

Create `apps/api/tests/test_agent_profiles_repo.py`:

```python
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import agent_profiles as repo
from usan_api.repositories.agent_profiles import ProfileInUseError
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


async def test_create_then_publish_increments_version(session_factory):
    async with session_factory() as db:
        profile = await repo.create_profile(
            db, name=_name(), description=None, actor_email="op"
        )
        await db.commit()
        pid = profile.id
        assert profile.published_version is None

    async with session_factory() as db:
        v1 = await repo.publish(db, pid, note="first", actor_email="op")
        await db.commit()
        assert v1 is not None and v1.version == 1

    async with session_factory() as db:
        v2 = await repo.publish(db, pid, note="second", actor_email="op")
        await db.commit()
        assert v2 is not None and v2.version == 2
        refreshed = await repo.get_profile(db, pid)
        assert refreshed is not None
        assert refreshed.published_version == 2


async def test_rollback_republishes_old_config(session_factory):
    async with session_factory() as db:
        profile = await repo.create_profile(
            db, name=_name(), description=None, actor_email="op"
        )
        pid = profile.id
        await repo.publish(db, pid, note="v1", actor_email="op")  # version 1
        changed = DEFAULT_AGENT_CONFIG.model_copy(
            update={"llm": DEFAULT_AGENT_CONFIG.llm.model_copy(update={"model": "x-2"})}
        )
        await repo.update_draft(
            db, pid, config=changed.model_dump(), description=None, actor_email="op"
        )
        await repo.publish(db, pid, note="v2", actor_email="op")  # version 2
        await db.commit()

    async with session_factory() as db:
        v3 = await repo.rollback(db, pid, target_version=1, actor_email="op")
        await db.commit()
        assert v3 is not None
        assert v3.version == 3  # rollback creates a NEW version
        assert v3.config["llm"]["model"] == "gemini-3.1-flash-lite"


async def test_set_default_is_exclusive_per_direction(session_factory):
    async with session_factory() as db:
        a = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
        b = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
        await db.commit()
        aid, bid = a.id, b.id

    async with session_factory() as db:
        await repo.set_default(db, aid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        await repo.set_default(db, bid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        a2 = await repo.get_profile(db, aid)
        b2 = await repo.get_profile(db, bid)
        assert a2 is not None and b2 is not None
        assert a2.is_default_outbound is False
        assert b2.is_default_outbound is True


async def test_archive_blocked_when_default(session_factory):
    async with session_factory() as db:
        p = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
        pid = p.id
        await repo.set_default(db, pid, direction="inbound")
        await db.commit()

    async with session_factory() as db:
        with pytest.raises(ProfileInUseError):
            await repo.archive_profile(db, pid)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_agent_profiles_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.repositories.agent_profiles'`

- [ ] **Step 3: Write the repository**

Create `apps/api/src/usan_api/repositories/agent_profiles.py`:

```python
import uuid
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, AgentProfileVersion, Elder
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG


class ProfileInUseError(Exception):
    """Raised when archiving a profile that is still a default or assigned to elders."""


async def create_profile(
    db: AsyncSession,
    *,
    name: str,
    description: str | None,
    actor_email: str,
    clone_from: uuid.UUID | None = None,
) -> AgentProfile:
    if clone_from is not None:
        source = await db.get(AgentProfile, clone_from)
        draft = (
            source.draft_config if source is not None else DEFAULT_AGENT_CONFIG.model_dump()
        )
    else:
        draft = DEFAULT_AGENT_CONFIG.model_dump()
    profile = AgentProfile(
        name=name,
        description=description,
        draft_config=draft,
        created_by=actor_email,
        updated_by=actor_email,
    )
    db.add(profile)
    await db.flush()
    await db.refresh(profile)
    return profile


async def get_profile(db: AsyncSession, profile_id: uuid.UUID) -> AgentProfile | None:
    return await db.get(AgentProfile, profile_id)


async def list_profiles(db: AsyncSession) -> list[AgentProfile]:
    result = await db.execute(select(AgentProfile).order_by(AgentProfile.name))
    return list(result.scalars().all())


async def update_draft(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    config: dict[str, Any],
    description: str | None,
    actor_email: str,
) -> AgentProfile | None:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    profile.draft_config = config
    if description is not None:
        profile.description = description
    profile.updated_by = actor_email
    await db.flush()
    await db.refresh(profile)
    return profile


async def _next_version(db: AsyncSession, profile_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.max(AgentProfileVersion.version)).where(
            AgentProfileVersion.profile_id == profile_id
        )
    )
    current = result.scalar_one_or_none()
    return (current or 0) + 1


async def publish(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    note: str | None,
    actor_email: str,
) -> AgentProfileVersion | None:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    version_number = await _next_version(db, profile_id)
    version = AgentProfileVersion(
        profile_id=profile_id,
        version=version_number,
        config=profile.draft_config,
        note=note,
        published_by=actor_email,
    )
    db.add(version)
    profile.published_version = version_number
    profile.updated_by = actor_email
    await db.flush()
    await db.refresh(version)
    return version


async def list_versions(
    db: AsyncSession, profile_id: uuid.UUID
) -> list[AgentProfileVersion]:
    result = await db.execute(
        select(AgentProfileVersion)
        .where(AgentProfileVersion.profile_id == profile_id)
        .order_by(AgentProfileVersion.version.desc())
    )
    return list(result.scalars().all())


async def get_version(
    db: AsyncSession, profile_id: uuid.UUID, version: int
) -> AgentProfileVersion | None:
    result = await db.execute(
        select(AgentProfileVersion).where(
            AgentProfileVersion.profile_id == profile_id,
            AgentProfileVersion.version == version,
        )
    )
    return result.scalar_one_or_none()


async def rollback(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    target_version: int,
    actor_email: str,
) -> AgentProfileVersion | None:
    target = await get_version(db, profile_id, target_version)
    if target is None:
        return None
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    # Copy the target snapshot back into the draft, then publish it as a NEW
    # version so history stays append-only and linear.
    profile.draft_config = target.config
    profile.updated_by = actor_email
    await db.flush()
    return await publish(
        db, profile_id, note=f"rollback to v{target_version}", actor_email=actor_email
    )


async def set_default(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    direction: Literal["inbound", "outbound"],
) -> AgentProfile | None:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    column = (
        AgentProfile.is_default_inbound
        if direction == "inbound"
        else AgentProfile.is_default_outbound
    )
    # Clear the current holder first (the partial-unique index forbids two trues).
    await db.execute(update(AgentProfile).where(column.is_(True)).values({column: False}))
    await db.flush()
    if direction == "inbound":
        profile.is_default_inbound = True
    else:
        profile.is_default_outbound = True
    await db.flush()
    await db.refresh(profile)
    return profile


async def count_assigned_elders(db: AsyncSession, profile_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Elder)
        .where(Elder.agent_profile_id == profile_id)
    )
    return int(result.scalar_one())


async def archive_profile(db: AsyncSession, profile_id: uuid.UUID) -> AgentProfile | None:
    profile = await db.get(AgentProfile, profile_id)
    if profile is None:
        return None
    if profile.is_default_inbound or profile.is_default_outbound:
        raise ProfileInUseError("profile is a live default; clear the default first")
    if await count_assigned_elders(db, profile_id) > 0:
        raise ProfileInUseError("profile is assigned to one or more elders")
    profile.status = ProfileStatus.ARCHIVED
    await db.flush()
    await db.refresh(profile)
    return profile


async def has_unpublished_draft(db: AsyncSession, profile: AgentProfile) -> bool:
    if profile.published_version is None:
        return True
    live = await get_version(db, profile.id, profile.published_version)
    if live is None:
        return True
    return bool(live.config != profile.draft_config)
```

- [ ] **Step 4: Run the repo tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_agent_profiles_repo.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/repositories/agent_profiles.py apps/api/tests/test_agent_profiles_repo.py
git commit -m "feat(api): agent-profile repository (publish/version/rollback/defaults)"
```

---

## Task 9: Actor seam

**Files:**
- Create: `apps/api/src/usan_api/admin_actor.py`

The admin routes are operator-token-guarded in P1; there is no per-user identity yet. This dependency returns a fixed actor email so audit/attribution code is already wired. P3 replaces the body with the SSO session email — no router changes needed then.

- [ ] **Step 1: Write the dependency**

Create `apps/api/src/usan_api/admin_actor.py`:

```python
"""Actor identity for admin mutations.

P1 has no per-user login (routes are operator-token-guarded), so attribution
records a fixed sentinel. P3 (Google SSO) swaps the body to return the
authenticated session email; callers and the audit log are unchanged.
"""

# Sentinel used until SSO lands (P3). Distinct from a real email so audit rows
# created pre-SSO are obvious.
OPERATOR_ACTOR = "operator-token"


def get_actor_email() -> str:
    return OPERATOR_ACTOR
```

- [ ] **Step 2: Verify import**

Run: `cd apps/api && uv run python -c "from usan_api.admin_actor import get_actor_email; print(get_actor_email())"`
Expected: `operator-token`

- [ ] **Step 3: Commit**

```bash
git add apps/api/src/usan_api/admin_actor.py
git commit -m "feat(api): admin actor-email seam (operator stub until SSO)"
```

---

## Task 10: Admin profiles router

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_profiles.py`
- Modify: `apps/api/src/usan_api/main.py`
- Test: `apps/api/tests/test_admin_profiles_api.py`

- [ ] **Step 1: Write the failing API test**

Create `apps/api/tests/test_admin_profiles_api.py`:

```python
import uuid

_OP = {"Authorization": "Bearer " + "o" * 32}


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


def test_create_profile_returns_201(client):
    r = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP)
    assert r.status_code == 201
    body = r.json()
    assert body["published_version"] is None
    assert body["has_unpublished_draft"] is True


def test_create_profile_requires_operator_token(client):
    r = client.post("/v1/admin/profiles", json={"name": _name()})
    assert r.status_code == 401


def test_create_duplicate_name_returns_409(client):
    name = _name()
    assert client.post("/v1/admin/profiles", json={"name": name}, headers=_OP).status_code == 201
    r = client.post("/v1/admin/profiles", json={"name": name}, headers=_OP)
    assert r.status_code == 409


def test_publish_then_list_versions(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    r = client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "first"}, headers=_OP)
    assert r.status_code == 201
    assert r.json()["version"] == 1
    versions = client.get(f"/v1/admin/profiles/{pid}/versions", headers=_OP).json()
    assert len(versions) == 1
    assert versions[0]["note"] == "first"


def test_edit_draft_then_get_reflects_change(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    detail = client.get(f"/v1/admin/profiles/{pid}", headers=_OP).json()
    cfg = detail["draft_config"]
    cfg["prompts"]["greeting"] = "Hi there, this is your check-in."
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg}, headers=_OP)
    assert r.status_code == 200
    assert r.json()["draft_config"]["prompts"]["greeting"] == "Hi there, this is your check-in."


def test_draft_rejects_brace_in_prompt(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    cfg = client.get(f"/v1/admin/profiles/{pid}", headers=_OP).json()["draft_config"]
    cfg["prompts"]["greeting"] = "Hello {name}"
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg}, headers=_OP)
    assert r.status_code == 422


def test_rollback_creates_new_version(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"}, headers=_OP)
    cfg = client.get(f"/v1/admin/profiles/{pid}", headers=_OP).json()["draft_config"]
    cfg["prompts"]["greeting"] = "Changed greeting here."
    client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg}, headers=_OP)
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v2"}, headers=_OP)
    r = client.post(f"/v1/admin/profiles/{pid}/rollback/1", json={}, headers=_OP)
    assert r.status_code == 201
    assert r.json()["version"] == 3


def test_set_default_exclusive(client):
    a = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    b = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    assert client.post(f"/v1/admin/profiles/{a}/set-default", json={"direction": "outbound"}, headers=_OP).status_code == 200
    assert client.post(f"/v1/admin/profiles/{b}/set-default", json={"direction": "outbound"}, headers=_OP).status_code == 200
    profiles = {p["id"]: p for p in client.get("/v1/admin/profiles", headers=_OP).json()}
    assert profiles[a]["is_default_outbound"] is False
    assert profiles[b]["is_default_outbound"] is True


def test_archive_blocked_when_default_returns_409(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/set-default", json={"direction": "inbound"}, headers=_OP)
    r = client.post(f"/v1/admin/profiles/{pid}/archive", json={}, headers=_OP)
    assert r.status_code == 409


def test_get_missing_profile_returns_404(client):
    r = client.get(f"/v1/admin/profiles/{uuid.uuid4()}", headers=_OP)
    assert r.status_code == 404
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_admin_profiles_api.py -v`
Expected: FAIL — 404s on every route (router not registered yet)

- [ ] **Step 3: Write the router**

Create `apps/api/src/usan_api/routers/admin_profiles.py`:

```python
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_operator_token
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import agent_profiles as repo
from usan_api.repositories.agent_profiles import ProfileInUseError
from usan_api.schemas.agent_profile import (
    DraftUpdate,
    ProfileCreate,
    ProfileDetail,
    ProfileSummary,
    PublishRequest,
    SetDefaultRequest,
    VersionDetail,
    VersionSummary,
)

router = APIRouter(
    prefix="/v1/admin/profiles",
    tags=["admin-profiles"],
    dependencies=[Depends(require_operator_token)],
)


@router.get("", response_model=list[ProfileSummary])
async def list_profiles(db: AsyncSession = Depends(get_db)) -> list[ProfileSummary]:
    profiles = await repo.list_profiles(db)
    summaries: list[ProfileSummary] = []
    for p in profiles:
        summaries.append(
            ProfileSummary.from_model(
                p,
                has_unpublished_draft=await repo.has_unpublished_draft(db, p),
                assigned_elder_count=await repo.count_assigned_elders(db, p.id),
            )
        )
    return summaries


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProfileSummary)
async def create_profile(
    body: ProfileCreate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> ProfileSummary:
    try:
        profile = await repo.create_profile(
            db,
            name=body.name,
            description=body.description,
            actor_email=actor,
            clone_from=body.clone_from,
        )
        await admin_audit.record(
            db,
            actor_email=actor,
            action="profile.create",
            entity_type="agent_profile",
            entity_id=str(profile.id),
            detail={"name": body.name},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="profile name already exists") from exc
    await db.refresh(profile)
    return ProfileSummary.from_model(
        profile, has_unpublished_draft=True, assigned_elder_count=0
    )


@router.get("/{profile_id}", response_model=ProfileDetail)
async def get_profile(
    profile_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ProfileDetail:
    profile = await repo.get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return ProfileDetail.from_model(profile)


@router.put("/{profile_id}/draft", response_model=ProfileDetail)
async def update_draft(
    profile_id: uuid.UUID,
    body: DraftUpdate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> ProfileDetail:
    profile = await repo.update_draft(
        db,
        profile_id,
        config=body.config.model_dump(),
        description=body.description,
        actor_email=actor,
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.draft_update",
        entity_type="agent_profile",
        entity_id=str(profile_id),
    )
    await db.commit()
    await db.refresh(profile)
    return ProfileDetail.from_model(profile)


@router.post(
    "/{profile_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=VersionSummary,
)
async def publish(
    profile_id: uuid.UUID,
    body: PublishRequest,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> VersionSummary:
    version = await repo.publish(db, profile_id, note=body.note, actor_email=actor)
    if version is None:
        raise HTTPException(status_code=404, detail="profile not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.publish",
        entity_type="agent_profile",
        entity_id=str(profile_id),
        detail={"version": version.version},
    )
    await db.commit()
    await db.refresh(version)
    return VersionSummary.from_model(version)


@router.get("/{profile_id}/versions", response_model=list[VersionSummary])
async def list_versions(
    profile_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[VersionSummary]:
    versions = await repo.list_versions(db, profile_id)
    return [VersionSummary.from_model(v) for v in versions]


@router.get("/{profile_id}/versions/{version}", response_model=VersionDetail)
async def get_version(
    profile_id: uuid.UUID, version: int, db: AsyncSession = Depends(get_db)
) -> VersionDetail:
    row = await repo.get_version(db, profile_id, version)
    if row is None:
        raise HTTPException(status_code=404, detail="version not found")
    return VersionDetail.from_model(row)


@router.post(
    "/{profile_id}/rollback/{version}",
    status_code=status.HTTP_201_CREATED,
    response_model=VersionSummary,
)
async def rollback(
    profile_id: uuid.UUID,
    version: int,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> VersionSummary:
    new_version = await repo.rollback(
        db, profile_id, target_version=version, actor_email=actor
    )
    if new_version is None:
        raise HTTPException(status_code=404, detail="profile or version not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.rollback",
        entity_type="agent_profile",
        entity_id=str(profile_id),
        detail={"from_version": version, "new_version": new_version.version},
    )
    await db.commit()
    await db.refresh(new_version)
    return VersionSummary.from_model(new_version)


@router.post("/{profile_id}/set-default", response_model=ProfileDetail)
async def set_default(
    profile_id: uuid.UUID,
    body: SetDefaultRequest,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> ProfileDetail:
    profile = await repo.set_default(db, profile_id, direction=body.direction)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.set_default",
        entity_type="agent_profile",
        entity_id=str(profile_id),
        detail={"direction": body.direction},
    )
    await db.commit()
    await db.refresh(profile)
    return ProfileDetail.from_model(profile)


@router.post("/{profile_id}/archive", response_model=ProfileDetail)
async def archive(
    profile_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> ProfileDetail:
    try:
        profile = await repo.archive_profile(db, profile_id)
    except ProfileInUseError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.archive",
        entity_type="agent_profile",
        entity_id=str(profile_id),
    )
    await db.commit()
    await db.refresh(profile)
    return ProfileDetail.from_model(profile)
```

- [ ] **Step 4: Register the router in `main.py`**

In `apps/api/src/usan_api/main.py`, add `admin_profiles` to the routers import (keep alphabetical):

```python
from usan_api.routers import admin_profiles, calls, dnc, elders, tools, webhooks
```

and add the include line in the same block as the other `include_router` calls, before `setup_metrics(app)`:

```python
    app.include_router(admin_profiles.router)
```

- [ ] **Step 5: Run the API tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_admin_profiles_api.py -v`
Expected: PASS (all tests)

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/routers/admin_profiles.py apps/api/src/usan_api/main.py apps/api/tests/test_admin_profiles_api.py
git commit -m "feat(api): admin profile endpoints (draft/publish/versions/rollback/defaults)"
```

---

## Task 11: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full API test suite**

Run: `cd apps/api && uv run pytest -v`
Expected: all tests pass (new + pre-existing). If a pre-existing test fails, it is almost certainly the conftest TRUNCATE change or a migration issue — fix before proceeding.

- [ ] **Step 2: Lint + format**

Run: `cd apps/api && ruff check . && ruff format --check .`
Expected: `All checks passed!` and no files needing format. If format differs, run `ruff format .` and re-commit.

- [ ] **Step 3: Type-check (CI runs this; not in CLAUDE.md)**

Run: `cd apps/api && uv run mypy`
Expected: `Success: no issues found`. Common fixes if it complains: ensure `from_model` return annotations are quoted strings, and that `repo`/schema function signatures match their call sites.

- [ ] **Step 4: Final commit if lint/format/mypy required changes**

```bash
git add -A
git commit -m "chore(api): lint/format/type fixes for admin profile foundation"
```

---

## Self-review notes (spec coverage)

This plan implements the P1 slice of `docs/superpowers/specs/2026-06-07-admin-ui-design.md`:

- **§5 Data model** — Tasks 1–3 (enums, models, migration) create `agent_profiles`, `agent_profile_versions`, `admin_users`, `admin_audit_log`, and the `elders.agent_profile_id` / `calls.profile_override` FKs. Refinement vs spec: the live version is tracked by an integer `published_version` (joined on `(profile_id, version)`) instead of a `published_version_id` FK — functionally identical, avoids a circular FK. Update the spec's §5 note when this merges.
- **§6.1 Authoring flow** — Task 8 (repo `publish`/`rollback`/`update_draft`) + Task 10 (endpoints).
- **§7 Prompt safety** — Task 5 (`_reject_braces`, slot whitelist) enforced on draft writes via Task 10's `PUT /draft`.
- **§8 Admin API** — Task 10 covers profiles list/create/get/draft/publish/versions/rollback/set-default/archive. **Deferred to a later P1-scope task or P3/P4:** `/elders` + `/elders/{id}/profile`, `/voices` (Cartesia proxy), `/audit` read endpoint, `/admin-users` CRUD. The audit *table + writer* exist now (Task 7) and every mutation writes to it; the read endpoint and admin-user management land with the UI/SSO phases.
- **Attribution** — Task 9 seam records `operator-token` until P3 swaps in the SSO email.

**Out of scope here (later phases):** the `runtime/agent-config` resolve endpoint + agent refactor (P2), Google SSO + real `require_admin_session` (P3), the React SPA (P4), infra/Caddy/Terraform (P5).

## Notes for the executor

- After all tasks pass, this is one squash-merged PR (scope `api`). Per the project's plan-PR workflow, branch from `origin/main`, and rebase any later admin-phase branch onto this once merged.
- Do **not** wire any agent changes here — the agent still reads its hardcoded constants until P2. This PR adds a dormant config store; nothing in production behavior changes.
