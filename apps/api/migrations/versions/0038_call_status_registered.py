"""add 'registered' to call_status enum (compat register-phone-call)"""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PG 12+: ADD VALUE is allowed in a transaction as long as the value is not USED in the
    # same migration (we only register it here). IF NOT EXISTS keeps re-runs safe.
    op.execute("ALTER TYPE call_status ADD VALUE IF NOT EXISTS 'registered'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; downgrade is a no-op (the value is harmless).
    pass
