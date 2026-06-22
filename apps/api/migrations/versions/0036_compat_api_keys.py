"""Feature 003: compat_api_keys table (RetellAI-compatible bearer keys).

Global, non-RLS control-plane table joined to organizations: a key is looked up by
prefix + constant-time hash compare BEFORE any org context exists, and the lookup then
OPENS the org-scoped RLS session for the resolved organization. Mirrors the invitations
(0035) global-table pattern — no RLS policy; app code scopes by organization_id.

Revision ID: 0036
Revises: 0035
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compat_api_keys",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        # First 8 chars of the token, plaintext — an O(1) candidate lookup + a display
        # hint. NOT unique (a prefix may legitimately recur); the constant-time hash
        # compare in compat/auth.py disambiguates and authenticates.
        sa.Column("key_prefix", sa.Text(), nullable=False),
        # sha256-hex of the FULL high-entropy token. The plaintext token is shown once at
        # create and never stored, mirroring the per-endpoint webhook signing-secret.
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Hot-path auth lookup: candidates by prefix (then hash-compared in app code).
    op.create_index("ix_compat_api_keys_key_prefix", "compat_api_keys", ["key_prefix"])
    # Admin listing is scoped per org.
    op.create_index("ix_compat_api_keys_organization_id", "compat_api_keys", ["organization_id"])

    # The non-superuser app role (RLS subject) must be able to use the table. Like
    # invitations / memberships, this is a GLOBAL control-plane table (no RLS policy): the
    # key is looked up BEFORE the org context exists, then app code scopes by
    # organization_id and opens the RLS-scoped session for the resolved org.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON compat_api_keys TO usan_app")


def downgrade() -> None:
    op.drop_index("ix_compat_api_keys_organization_id", table_name="compat_api_keys")
    op.drop_index("ix_compat_api_keys_key_prefix", table_name="compat_api_keys")
    op.drop_table("compat_api_keys")
