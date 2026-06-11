"""outbound webhooks: webhook_endpoints + webhook_deliveries transactional outbox

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-10

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Operator-registered webhook destinations. secret = 64 hex chars
    # (32 random bytes), server-generated, returned once at create, never logged.
    # events is a subscription list constrained to the closed event enum;
    # disabled_reason marks circuit-breaker auto-disables (operator disables via
    # enabled=false keep it NULL, so the two are distinguishable).
    op.execute(
        """
        CREATE TABLE webhook_endpoints (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            url TEXT NOT NULL,
            description TEXT,
            enabled BOOLEAN NOT NULL DEFAULT true,
            secret TEXT NOT NULL,
            events TEXT[] NOT NULL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_webhook_endpoints_events CHECK (
                cardinality(events) >= 1
                AND events <@ ARRAY[
                    'call.started', 'call.completed', 'flag.created',
                    'callback.created', 'batch.completed'
                ]::TEXT[]
            ),
            CONSTRAINT ck_webhook_endpoints_disabled_reason CHECK (
                disabled_reason IS NULL OR disabled_reason IN ('circuit_breaker')
            ),
            CONSTRAINT ck_webhook_endpoints_failures CHECK (consecutive_failures >= 0)
        )
        """
    )

    # 2. Transactional outbox: one row per (event occurrence x subscribed endpoint),
    # inserted in the SAME transaction as the state change it announces.
    # 'ping' is valid as a delivery event (the /test endpoint) but is deliberately
    # absent from the subscription CHECK above.
    # last_error: exception TYPE NAME only, never str(exc) (PHI-adjacent rule).
    op.execute(
        """
        CREATE TABLE webhook_deliveries (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            endpoint_id UUID NOT NULL
                REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
            event TEXT NOT NULL,
            payload JSONB NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            response_code INTEGER,
            last_error TEXT,
            delivered_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_webhook_deliveries_status CHECK (
                status IN ('pending', 'delivered', 'failed')
            ),
            CONSTRAINT ck_webhook_deliveries_event CHECK (
                event IN (
                    'call.started', 'call.completed', 'flag.created',
                    'callback.created', 'batch.completed', 'ping'
                )
            ),
            CONSTRAINT ck_webhook_deliveries_attempts CHECK (attempts >= 0)
        )
        """
    )

    # 3. Claim index: the poller predicate is
    # (status = 'pending' AND next_attempt_at <= now()), so a partial index on
    # pending rows keeps claims O(due) regardless of delivered/failed history.
    op.execute(
        """
        CREATE INDEX idx_webhook_deliveries_due
            ON webhook_deliveries (next_attempt_at)
            WHERE status = 'pending'
        """
    )

    # 4. Operator deliveries list: GET /v1/webhook-endpoints/{id}/deliveries
    # orders newest-first per endpoint; (created_at, id) pair because created_at
    # ties are guaranteed (func.now() is the transaction timestamp and fan-out
    # inserts one row per endpoint in one transaction).
    op.execute(
        """
        CREATE INDEX idx_webhook_deliveries_endpoint
            ON webhook_deliveries (endpoint_id, created_at DESC, id DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_webhook_deliveries_endpoint")
    op.execute("DROP INDEX IF EXISTS idx_webhook_deliveries_due")
    op.execute("DROP TABLE IF EXISTS webhook_deliveries")
    op.execute("DROP TABLE IF EXISTS webhook_endpoints")
