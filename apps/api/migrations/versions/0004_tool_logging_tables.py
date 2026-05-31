"""tool-logging tables: transcripts, wellness_logs, medication_logs

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-31

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE transcripts (
            id          BIGSERIAL PRIMARY KEY,
            call_id     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            tool_name   TEXT,
            tool_args   JSONB,
            started_at  TIMESTAMPTZ NOT NULL,
            ended_at    TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_transcripts_call ON transcripts(call_id, started_at)")

    op.execute(
        """
        CREATE TABLE wellness_logs (
            id            BIGSERIAL PRIMARY KEY,
            call_id       UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id      UUID NOT NULL REFERENCES elders(id),
            mood          SMALLINT,
            pain_level    SMALLINT,
            notes         TEXT,
            raw           JSONB NOT NULL DEFAULT '{}',
            logged_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE medication_logs (
            id              BIGSERIAL PRIMARY KEY,
            call_id         UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id        UUID NOT NULL REFERENCES elders(id),
            medication_name TEXT NOT NULL,
            taken           BOOLEAN NOT NULL,
            reported_time   TIMESTAMPTZ,
            logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS medication_logs")
    op.execute("DROP TABLE IF EXISTS wellness_logs")
    op.execute("DROP TABLE IF EXISTS transcripts")
