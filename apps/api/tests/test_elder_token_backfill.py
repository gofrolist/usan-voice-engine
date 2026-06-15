"""Migration 0028 backfills legacy ``elder_name`` prompt tokens to ``contact_name``.

0027 renamed the schema but left the Elder->Contact references that live as
substitution tokens *inside* the agent-config JSONB. A profile authored before the
rename keeps ``{elder_name}`` / ``{{elder_name}}`` in its prompt text, and on read
that config fails ``AgentConfig`` validation (stray ``{``) -> 500. 0028 completes the
rename in the data; this test seeds the legacy spellings and asserts the transform is
correct, scoped, idempotent, and leaves the JSONB valid.

The UPDATE statements mirror ``migrations/versions/0028_backfill_elder_prompt_tokens``;
keep them in lockstep with that revision.
"""

import asyncio
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Mirror of 0028._BACKFILL_SQL (draft_config branch). Double-brace first so the
# single-brace pass cannot corrupt a {{...}} token; WHERE scopes to legacy rows.
_BACKFILL_DRAFT = (
    "UPDATE agent_profiles SET draft_config = "
    "replace(replace(draft_config::text, '{{elder_name}}', '{{contact_name}}'), "
    "'{elder_name}', '{contact_name}')::jsonb "
    "WHERE draft_config::text LIKE '%elder_name%'"
)

_LEGACY = "Hi {elder_name}, also {{elder_name}}, and {last_check_in_line}."
_EXPECTED = "Hi {contact_name}, also {{contact_name}}, and {last_check_in_line}."
_CLEAN = "Hi {contact_name}, nothing legacy here."


async def _run(async_database_url: str) -> tuple[str, str, int]:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    legacy_name = "ztest-backfill-legacy"
    clean_name = "ztest-backfill-clean"
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM agent_profiles WHERE name IN (:a, :b)"),
                {"a": legacy_name, "b": clean_name},
            )
            for nm, tmpl in ((legacy_name, _LEGACY), (clean_name, _CLEAN)):
                await conn.execute(
                    text(
                        "INSERT INTO agent_profiles (name, draft_config) "
                        "VALUES (:n, CAST(:c AS jsonb))"
                    ),
                    {
                        "n": nm,
                        "c": json.dumps({"prompts": {"inbound_personalization_template": tmpl}}),
                    },
                )
            # Run the backfill twice — the second pass must be a no-op (idempotent).
            await conn.execute(text(_BACKFILL_DRAFT))
            await conn.execute(text(_BACKFILL_DRAFT))

        async with engine.connect() as conn:

            def _tmpl(name: str):
                return text(
                    "SELECT draft_config->'prompts'->>'inbound_personalization_template' "
                    "FROM agent_profiles WHERE name = :n"
                ).bindparams(n=name)

            legacy_after = (await conn.execute(_tmpl(legacy_name))).scalar_one()
            clean_after = (await conn.execute(_tmpl(clean_name))).scalar_one()
            remaining = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM agent_profiles "
                        "WHERE draft_config::text LIKE '%elder_name%'"
                    )
                )
            ).scalar_one()
        return legacy_after, clean_after, remaining
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM agent_profiles WHERE name IN (:a, :b)"),
                {"a": legacy_name, "b": clean_name},
            )
        await engine.dispose()


def test_backfill_rewrites_elder_tokens(async_database_url):
    legacy_after, clean_after, remaining = asyncio.run(_run(async_database_url))
    # Both brace spellings rewritten; the non-elder legacy slot is untouched.
    assert legacy_after == _EXPECTED
    # A row that never had the legacy token is left exactly as-is.
    assert clean_after == _CLEAN
    # No elder_name token remains anywhere (idempotent: two passes, still clean).
    assert remaining == 0
