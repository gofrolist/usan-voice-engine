"""custom variables: operator-declared prompt-variable catalog

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-10

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Operator-declared prompt variables (catalog tier "custom"). Definitions are
    # documentation/UX only — values arrive per call via Call.dynamic_vars. name is
    # immutable after create (rename would silently orphan {{tokens}} in templates).
    # Collision with the 10 frozen builtin names is enforced in the Pydantic layer.
    op.execute(
        """
        CREATE TABLE custom_variables (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            example TEXT NOT NULL DEFAULT '',
            phi BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_custom_variables_name_slug CHECK (name ~ '^[a-z][a-z0-9_]{0,63}$')
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS custom_variables")
