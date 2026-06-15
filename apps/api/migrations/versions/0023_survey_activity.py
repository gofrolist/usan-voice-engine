"""wellbeing_survey_results + activity_history (US6 / Clara Care Parity 002)

The monthly wellbeing survey (``wellbeing_survey_results``, one row per elder per calendar
month — unique ``(elder_id, period_month)`` makes ``record_survey`` once-per-month
idempotent, FR-032 / SC-008) plus the per-elder mood-boosting activity usage log
(``activity_history`` — the catalog itself is code; this only tracks which activity was
used when, so ``get_activity`` can avoid recent repeats, FR-034 / SC-009). Both ``call_id``
FKs are SET NULL on call delete so a PHI retention purge keeps the aggregate/recency signal
but drops the back-link. Additive; no existing data is touched.

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # wellbeing_survey_results: BigInteger PK. period_month is a first-of-month DATE anchor;
    # the unique (elder_id, period_month) is the once-per-month guard. Scores are SMALLINT
    # and nullable (the elder may answer only some). call_id SET NULL on delete (no ondelete
    # CASCADE — the monthly aggregate outlives a purged call).
    op.execute(
        "CREATE TABLE wellbeing_survey_results ("
        "id BIGSERIAL PRIMARY KEY, "
        "call_id UUID REFERENCES calls(id) ON DELETE SET NULL, "
        "elder_id UUID NOT NULL REFERENCES elders(id), "
        "period_month DATE NOT NULL, "
        "loneliness SMALLINT, "
        "mood SMALLINT, "
        "satisfaction SMALLINT, "
        "raw JSONB NOT NULL DEFAULT '{}', "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "CONSTRAINT uq_wellbeing_survey_elder_month UNIQUE (elder_id, period_month)"
        ")"
    )

    # activity_history: BigInteger PK (high-volume child rows). activity_key is a free Text
    # reference into activities_catalog.py (no FK — the catalog is code, not a table). The
    # (elder_id, used_at DESC) index serves the least-recently-used selection scan.
    op.execute(
        "CREATE TABLE activity_history ("
        "id BIGSERIAL PRIMARY KEY, "
        "elder_id UUID NOT NULL REFERENCES elders(id), "
        "activity_key TEXT NOT NULL, "
        "call_id UUID REFERENCES calls(id) ON DELETE SET NULL, "
        "used_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")"
    )
    op.execute(
        "CREATE INDEX ix_activity_history_elder_recent ON activity_history(elder_id, used_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS activity_history")
    op.execute("DROP TABLE IF EXISTS wellbeing_survey_results")
