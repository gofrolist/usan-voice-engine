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
    op.execute("CREATE INDEX idx_admin_audit_log_created ON admin_audit_log(created_at DESC)")


def downgrade() -> None:
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS profile_override")
    op.execute("ALTER TABLE elders DROP COLUMN IF EXISTS agent_profile_id")
    op.execute("DROP TABLE IF EXISTS admin_audit_log CASCADE")
    op.execute("DROP TABLE IF EXISTS admin_users CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_profile_versions CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_profiles CASCADE")
    op.execute("DROP TYPE IF EXISTS admin_role")
    op.execute("DROP TYPE IF EXISTS profile_status")
