"""knowledge bases: 3 TenantScoped + FORCE-RLS tables + pgvector + cross-org claim fn (Phase 5).

Owner-DDL: CREATE EXTENSION vector + FORCE RLS + GRANT usan_app + a SECURITY DEFINER claim
function (the only cross-org primitive — the ingestion poller runs as least-priv usan_app and
cannot SELECT across orgs). Additive + inert until a v* tag.

Revision ID: 0047
Revises: 0046
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_DEFAULT_EXPR = "COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (organization_id = current_setting('app.current_org', true)::uuid) "
        f"WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO usan_app")


def _org_col() -> sa.Column:
    return sa.Column(
        "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
    )


def _id_col() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _ts(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_bases",
        _id_col(),
        _org_col(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'in_progress'"), nullable=False),
        sa.Column("max_chunk_size", sa.Integer(), nullable=False),
        sa.Column("min_chunk_size", sa.Integer(), nullable=False),
        sa.Column(
            "enable_auto_refresh", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_bases_organization_id", "knowledge_bases", ["organization_id"])
    # Serves the claim predicate (status + lease) without a seq scan.
    op.create_index(
        "ix_knowledge_bases_claim", "knowledge_bases", ["status", "claimed_at", "created_at"]
    )

    op.create_table(
        "knowledge_base_sources",
        _id_col(),
        _org_col(),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_url", sa.Text(), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_base_sources_organization_id", "knowledge_base_sources", ["organization_id"]
    )
    op.create_index("ix_knowledge_base_sources_kb", "knowledge_base_sources", ["knowledge_base_id"])

    op.create_table(
        "knowledge_base_chunks",
        _id_col(),
        _org_col(),
        sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["knowledge_base_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "chunk_index", name="uq_knowledge_base_chunks_source_chunk"
        ),
    )
    op.create_index(
        "ix_knowledge_base_chunks_organization_id", "knowledge_base_chunks", ["organization_id"]
    )
    op.create_index("ix_knowledge_base_chunks_source", "knowledge_base_chunks", ["source_id"])
    op.execute(
        "CREATE INDEX ix_knowledge_base_chunks_embedding_hnsw ON knowledge_base_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # Cross-org lease-claim. SECURITY DEFINER (owner) bypasses RLS to see all orgs; returns ids
    # only (no PHI); explicit search_path (definer hygiene). GRANT EXECUTE to usan_app.
    op.execute(
        """
        CREATE FUNCTION claim_pending_knowledge_bases(p_limit int, p_lease_seconds int)
        RETURNS TABLE(id uuid, organization_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          UPDATE knowledge_bases SET claimed_at = now()
          WHERE id IN (
            SELECT kb.id FROM knowledge_bases kb
            WHERE kb.status = 'in_progress'
              AND (kb.claimed_at IS NULL
                   OR kb.claimed_at < now() - make_interval(secs => p_lease_seconds))
            ORDER BY kb.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT p_limit
          )
          RETURNING knowledge_bases.id, knowledge_bases.organization_id;
        $$
        """
    )
    op.execute("GRANT EXECUTE ON FUNCTION claim_pending_knowledge_bases(int, int) TO usan_app")

    for t in ("knowledge_bases", "knowledge_base_sources", "knowledge_base_chunks"):
        _enable_rls(t)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS claim_pending_knowledge_bases(int, int)")
    for t in ("knowledge_base_chunks", "knowledge_base_sources", "knowledge_bases"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
    op.drop_table("knowledge_base_chunks")
    op.drop_table("knowledge_base_sources")
    op.drop_table("knowledge_bases")
    # Do NOT drop the vector extension (future objects may depend on it).
