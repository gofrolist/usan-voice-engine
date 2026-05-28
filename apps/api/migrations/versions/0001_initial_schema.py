"""initial schema: elders, dnc_list, calls

Revision ID: 0001
Revises:
Create Date: 2026-05-28

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE elders (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            external_id     TEXT UNIQUE,
            name            TEXT NOT NULL,
            phone_e164      TEXT NOT NULL UNIQUE,
            timezone        TEXT NOT NULL,
            preferred_voice TEXT,
            metadata        JSONB NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE dnc_list (
            phone_e164  TEXT PRIMARY KEY,
            reason      TEXT,
            added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute("CREATE TYPE call_direction AS ENUM ('outbound', 'inbound')")
    op.execute(
        """
        CREATE TYPE call_status AS ENUM (
            'queued', 'dialing', 'ringing', 'in_progress',
            'completed', 'voicemail_left', 'no_answer',
            'busy', 'failed', 'dnc_blocked', 'cancelled'
        )
        """
    )

    op.execute(
        """
        CREATE TABLE calls (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            elder_id           UUID REFERENCES elders(id) ON DELETE SET NULL,
            direction          call_direction NOT NULL,
            status             call_status NOT NULL DEFAULT 'queued',
            idempotency_key    TEXT UNIQUE,
            livekit_room       TEXT,
            sip_call_id        TEXT,
            dynamic_vars       JSONB NOT NULL DEFAULT '{}',
            parent_call_id     UUID REFERENCES calls(id),
            attempt            SMALLINT NOT NULL DEFAULT 1,
            scheduled_at       TIMESTAMPTZ,
            started_at         TIMESTAMPTZ,
            answered_at        TIMESTAMPTZ,
            ended_at           TIMESTAMPTZ,
            duration_seconds   INTEGER,
            end_reason         TEXT,
            recording_uri      TEXT,
            error              JSONB,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute("CREATE INDEX idx_calls_elder ON calls(elder_id, created_at DESC)")
    op.execute(
        """
        CREATE INDEX idx_calls_status_scheduled ON calls(status, scheduled_at)
            WHERE status IN ('queued', 'no_answer', 'voicemail_left')
        """
    )
    op.execute("CREATE INDEX idx_calls_livekit_room ON calls(livekit_room)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS calls")
    op.execute("DROP TYPE IF EXISTS call_status")
    op.execute("DROP TYPE IF EXISTS call_direction")
    op.execute("DROP TABLE IF EXISTS dnc_list")
    op.execute("DROP TABLE IF EXISTS elders")
