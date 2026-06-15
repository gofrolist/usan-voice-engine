"""backfill elder_name -> contact_name tokens inside stored prompt JSONB

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-15

Migration 0027 flipped the *schema* (elders -> contacts, elder_id -> contact_id) but
did NOT rewrite the Elder->Contact references embedded as substitution tokens inside
the agent-config JSONB. Profiles authored before the rename keep the historical
``{elder_name}`` / ``{{elder_name}}`` tokens in their prompt text. On read those
configs deserialize through ``AgentConfig``, whose post-rename validator only tolerates
the ``{contact_name}`` legacy single-brace slot, so the stray ``{`` left by
``{elder_name}`` raised ValidationError -> 500 on ``GET /v1/admin/profiles/{id}``.

This completes the rename in the DATA: it rewrites both token spellings to
``contact_name`` across ``agent_profiles.draft_config`` and
``agent_profile_versions.config``. Idempotent — double-brace first so the single-brace
pass can't corrupt a ``{{...}}`` token, and the WHERE scopes it to rows that still
carry the legacy spelling, so it is safe to re-run and a no-op once clean.

Forward-only: ``downgrade`` is intentionally a no-op. The pre-rename spelling is
unrecoverable (a post-rename ``{{contact_name}}`` is indistinguishable from a migrated
one), and 0027's downgrade already reverses the schema half of the rename.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain SQL (braces are literal — NOT str.format/f-string templates). The table/column
# names are fixed constants, never user input. Mirrored by
# tests/test_elder_token_backfill.py, which asserts the same transform + idempotency.
_BACKFILL_SQL: tuple[str, ...] = (
    "UPDATE agent_profiles SET draft_config = "
    "replace(replace(draft_config::text, '{{elder_name}}', '{{contact_name}}'), "
    "'{elder_name}', '{contact_name}')::jsonb "
    "WHERE draft_config::text LIKE '%elder_name%'",
    "UPDATE agent_profile_versions SET config = "
    "replace(replace(config::text, '{{elder_name}}', '{{contact_name}}'), "
    "'{elder_name}', '{contact_name}')::jsonb "
    "WHERE config::text LIKE '%elder_name%'",
)


def upgrade() -> None:
    for stmt in _BACKFILL_SQL:
        op.execute(stmt)


def downgrade() -> None:
    # Forward-only data completion: the original token spelling cannot be recovered
    # unambiguously. Schema reversal is owned by 0027's downgrade.
    pass
