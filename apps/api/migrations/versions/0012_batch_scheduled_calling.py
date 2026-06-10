"""batch & scheduled calling: call_schedules, call_batches, call_batch_targets

Plus idx_calls_in_flight on calls (concurrency-gate count, spec §3.4).

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-10

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # call_schedules: recurring per-elder daily wellness call (spec §3.1).
    # One schedule per elder, enforced by UNIQUE (elder_id).
    op.execute(
        """
        CREATE TABLE call_schedules (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            -- CASCADE: a schedule is meaningless without its elder; calls.elder_id
            -- already SET NULLs independently, so history survives.
            elder_id               UUID NOT NULL UNIQUE REFERENCES elders(id) ON DELETE CASCADE,
            enabled                BOOLEAN NOT NULL DEFAULT true,
            window_start_local     TIME NOT NULL,            -- elder-local wall clock
            window_end_local       TIME NOT NULL,
            days_of_week           SMALLINT NOT NULL DEFAULT 127,  -- bit 0=Mon ... bit 6=Sun
            dynamic_vars           JSONB NOT NULL DEFAULT '{}',    -- 8 KB cap (schema layer)
            profile_override       UUID REFERENCES agent_profiles(id) ON DELETE SET NULL,
            next_run_at            TIMESTAMPTZ NOT NULL,     -- computed in Python (zoneinfo)
            last_materialized_date DATE,                     -- elder-local date last fired
            last_result            TEXT,                     -- per-elder skip observability
            last_result_at         TIMESTAMPTZ,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_call_schedules_window CHECK (window_start_local < window_end_local),
            CONSTRAINT ck_call_schedules_days   CHECK (days_of_week BETWEEN 1 AND 127),
            CONSTRAINT ck_call_schedules_result CHECK (last_result IS NULL OR last_result IN
                ('created','replayed','rescheduled','skipped_window','skipped_invalid_timezone',
                 'skipped_daily_cap','dnc_blocked','key_conflict'))
        )
        """
    )
    # The poller's exact claim predicate (idx_calls_due_retries precedent):
    op.execute("CREATE INDEX idx_call_schedules_due ON call_schedules (next_run_at) WHERE enabled")

    # call_batches: one-off campaigns that materialize ordinary Call rows (spec §3.2).
    op.execute(
        """
        CREATE TABLE call_batches (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name                TEXT NOT NULL,    -- operator label; PHI-free by convention
            idempotency_key     TEXT UNIQUE,      -- optional create-replay guard
            payload_digest      TEXT NOT NULL,    -- sha256 of canonical payload (replay)
            status              TEXT NOT NULL DEFAULT 'scheduled',
            trigger_at          TIMESTAMPTZ,      -- NULL = next poll cycle
            window_start_local  TIME,             -- optional per-elder-local window
            window_end_local    TIME,
            days_of_week        SMALLINT,         -- NULL = any day
            max_concurrency     SMALLINT,         -- materialization throttle, NOT a dial cap
            profile_override    UUID REFERENCES agent_profiles(id) ON DELETE SET NULL,
            started_at          TIMESTAMPTZ,
            completed_at        TIMESTAMPTZ,      -- also stamped on drained cancelled batches
            cancelled_at        TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_call_batches_status CHECK
                (status IN ('scheduled','running','completed','cancelled')),
            CONSTRAINT ck_call_batches_window CHECK (
                ((window_start_local IS NULL) = (window_end_local IS NULL))
                AND (window_start_local IS NULL OR window_start_local < window_end_local)),
            CONSTRAINT ck_call_batches_days CHECK
                (days_of_week IS NULL OR days_of_week BETWEEN 1 AND 127),
            CONSTRAINT ck_call_batches_maxconc CHECK
                (max_concurrency IS NULL OR max_concurrency >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_call_batches_due ON call_batches (trigger_at) WHERE status = 'scheduled'"
    )
    # "Open" working set for the poller: running batches, plus cancelled batches that
    # still have unsettled targets. completed_at IS NULL is the exit condition — a
    # cancelled batch is stamped completed_at once drained and leaves this index
    # forever (the sweep working set is bounded, not monotonic).
    op.execute(
        """
        CREATE INDEX idx_call_batches_open ON call_batches (created_at)
            WHERE status IN ('running','cancelled') AND completed_at IS NULL
        """
    )

    # call_batch_targets: one row per submitted target (spec §3.3).
    op.execute(
        """
        CREATE TABLE call_batch_targets (
            id               BIGSERIAL PRIMARY KEY,
            batch_id         UUID NOT NULL REFERENCES call_batches(id) ON DELETE CASCADE,
            target_index     INTEGER NOT NULL,        -- position in the submitted array
            -- SET NULL (not CASCADE): a deleted elder must not silently shrink the
            -- batch; the poller marks the orphan target skipped/elder_deleted instead.
            elder_id         UUID REFERENCES elders(id) ON DELETE SET NULL,
            dynamic_vars     JSONB NOT NULL DEFAULT '{}',
            profile_override UUID REFERENCES agent_profiles(id) ON DELETE SET NULL,
            status           TEXT NOT NULL DEFAULT 'pending',
            -- skip_reason: elder_deleted | invalid_timezone | key_conflict | daily_cap
            skip_reason      TEXT,
            call_id          UUID REFERENCES calls(id) ON DELETE SET NULL,  -- root attempt
            final_status     TEXT,           -- terminal CallStatus of the LAST attempt
            materialized_at  TIMESTAMPTZ,
            finalized_at     TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_call_batch_targets_idx UNIQUE (batch_id, target_index),
            CONSTRAINT ck_call_batch_targets_status CHECK
                (status IN ('pending','materialized','done','skipped','cancelled'))
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_call_batch_targets_pending ON call_batch_targets (batch_id, target_index)
            WHERE status = 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX idx_call_batch_targets_open ON call_batch_targets (batch_id)
            WHERE status IN ('pending','materialized')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_call_batch_targets_call ON call_batch_targets (call_id)
            WHERE call_id IS NOT NULL
        """
    )

    # Concurrency gate (spec §5.4): counting dial-slot consumers must not scan the
    # monotonically-growing calls table, and the count is RECENCY-BOUNDED (a row
    # stuck IN_PROGRESS by a lost room_finished webhook must not consume a slot
    # forever), so the index key is updated_at under the static status predicate.
    # RINGING is included defensively (enum value, no writer yet).
    op.execute(
        """
        CREATE INDEX idx_calls_in_flight ON calls (updated_at)
            WHERE status IN ('dialing','ringing','in_progress')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_in_flight")
    op.execute("DROP TABLE IF EXISTS call_batch_targets")
    op.execute("DROP TABLE IF EXISTS call_batches")
    op.execute("DROP TABLE IF EXISTS call_schedules")
