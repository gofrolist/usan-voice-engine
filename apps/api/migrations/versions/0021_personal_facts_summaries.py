"""personal_facts + conversation_summaries (US4 / Clara Care Parity 002)

Durable, categorized memory about an elder (``personal_facts``) plus a per-call
carry-forward recap (``conversation_summaries``). Both feed the memory built-ins
(personal_facts / last_call_summary / open_plans / important_dates) injected into the
next call. ``conversation_summaries.call_id`` is UNIQUE so the post-call summarization
trigger is idempotent (one summary per call). Additive; no existing data is touched.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-14

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # personal_facts: BigInteger PK (high-volume child rows). category/source are
    # Text+CHECK so the sets widen without an ORM recompile (mirrors family_tasks). No
    # ondelete on elder_id — a fact's context outlives an elder row change. phi defaults
    # true (Constitution II: protected unless proven otherwise). active=false marks a
    # superseded fact (update-not-duplicate); only active rows are injected.
    op.execute(
        "CREATE TABLE personal_facts ("
        "id BIGSERIAL PRIMARY KEY, "
        "elder_id UUID NOT NULL REFERENCES elders(id), "
        "category TEXT NOT NULL, "
        "content TEXT NOT NULL, "
        "structured JSONB NOT NULL DEFAULT '{}', "
        "source TEXT NOT NULL DEFAULT 'elder_stated', "
        "active BOOLEAN NOT NULL DEFAULT true, "
        "phi BOOLEAN NOT NULL DEFAULT true, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "CONSTRAINT ck_personal_facts_category CHECK "
        "(category IN ('person', 'routine', 'preference', 'important_date', 'health_context')), "
        "CONSTRAINT ck_personal_facts_source CHECK "
        "(source IN ('operator', 'elder_stated', 'extracted'))"
        ")"
    )
    op.execute("CREATE INDEX ix_personal_facts_elder_active ON personal_facts(elder_id, active)")
    op.execute(
        "CREATE INDEX ix_personal_facts_elder_category ON personal_facts(elder_id, category)"
    )

    # conversation_summaries: one recap per completed call. call_id UNIQUE makes the
    # post-call summarization trigger idempotent (a re-fired end_call/room_finished
    # webhook re-inserts nothing). ondelete CASCADE so PHI retention purging a call also
    # drops its summary. open_plans is a JSON array of stated follow-ups.
    op.execute(
        "CREATE TABLE conversation_summaries ("
        "id BIGSERIAL PRIMARY KEY, "
        "call_id UUID NOT NULL UNIQUE REFERENCES calls(id) ON DELETE CASCADE, "
        "elder_id UUID NOT NULL REFERENCES elders(id), "
        "summary TEXT NOT NULL, "
        "open_plans JSONB NOT NULL DEFAULT '[]', "
        "model_version TEXT NOT NULL DEFAULT '', "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")"
    )
    op.execute(
        "CREATE INDEX ix_conversation_summaries_elder_recent "
        "ON conversation_summaries(elder_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS conversation_summaries")
    op.execute("DROP TABLE IF EXISTS personal_facts")
