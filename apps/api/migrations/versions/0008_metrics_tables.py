"""metrics tables: turn_metrics (per-turn latency), call_metrics (per-call cost/usage)

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-05

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE turn_metrics (
            id                     BIGSERIAL PRIMARY KEY,
            call_id                UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            turn_index             INTEGER NOT NULL,
            eou_delay_ms           INTEGER,
            transcription_delay_ms INTEGER,
            stt_duration_ms        INTEGER,
            llm_ttft_ms            INTEGER,
            tts_ttfb_ms            INTEGER,
            llm_completion_tokens  INTEGER,
            tts_characters         INTEGER,
            response_latency_ms    INTEGER,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_turn_metrics_call_turn ON turn_metrics(call_id, turn_index)")
    op.execute("CREATE INDEX idx_turn_metrics_created ON turn_metrics(created_at)")

    op.execute(
        """
        CREATE TABLE call_metrics (
            call_id               UUID PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
            llm_prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            llm_completion_tokens INTEGER NOT NULL DEFAULT 0,
            llm_total_tokens      INTEGER NOT NULL DEFAULT 0,
            tts_characters        INTEGER NOT NULL DEFAULT 0,
            stt_audio_seconds     NUMERIC(10,2) NOT NULL DEFAULT 0,
            duration_seconds      INTEGER,
            cost_telephony_usd    NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_llm_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_stt_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_tts_usd          NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_storage_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
            cost_total_usd        NUMERIC(12,6) NOT NULL DEFAULT 0,
            pricing_version       TEXT NOT NULL,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_call_metrics_created ON call_metrics(created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS turn_metrics CASCADE")
    op.execute("DROP TABLE IF EXISTS call_metrics CASCADE")
