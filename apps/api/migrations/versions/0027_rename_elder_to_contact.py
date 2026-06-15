"""rename elder -> contact across the schema (entity genericization)

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-15

The product is no longer elder-care-specific: the primary person is now a generic
``Contact``. This flips the LIVE schema to match — the ``elders`` table -> ``contacts``,
every ``elder_id`` FK column -> ``contact_id``, and the ``personal_facts.source`` value
``elder_stated`` -> ``contact_stated``. ``family_contacts`` (the relatives who receive
alerts) is a DISTINCT entity and is intentionally NOT renamed; only its ``elder_id`` FK
column becomes ``contact_id``.

Scope note: this renames the table, columns and stored value — the surface the
application/API/UI and all code use. It deliberately does NOT rename internal index/
constraint NAMES (e.g. ``idx_contacts_phone`` stays ``idx_elders_phone``,
``uq_call_schedules_elder_slot``). Those names are invisible to the application, and
renaming them cannot be reversed safely alongside the ``family_contacts`` objects that
legitimately contain ``contact``; leaving them also keeps every older migration's
downgrade (which drops them by their original ``elder`` name) working. The historical
migrations 0001-0026 keep their ``elders``/``elder_id`` DDL (immutable ledger); this is
the single point where the live schema flips, and it round-trips cleanly.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENAME_ELDER_ID_TO_CONTACT_ID = """
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT table_name FROM information_schema.columns
           WHERE table_schema='public' AND column_name='elder_id' LOOP
    EXECUTE format('ALTER TABLE %I RENAME COLUMN elder_id TO contact_id', r.table_name);
  END LOOP;
END $$;
"""

_RENAME_CONTACT_ID_TO_ELDER_ID = """
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT table_name FROM information_schema.columns
           WHERE table_schema='public' AND column_name='contact_id' LOOP
    EXECUTE format('ALTER TABLE %I RENAME COLUMN contact_id TO elder_id', r.table_name);
  END LOOP;
END $$;
"""


def upgrade() -> None:
    op.execute("ALTER TABLE elders RENAME TO contacts")
    op.execute(_RENAME_ELDER_ID_TO_CONTACT_ID)
    # personal_facts.source: migrate the stored value + swap its CHECK + default
    op.execute("UPDATE personal_facts SET source='contact_stated' WHERE source='elder_stated'")
    op.execute(
        """
        DO $$ DECLARE r record; BEGIN
          FOR r IN SELECT conname FROM pg_constraint
                   WHERE conrelid='personal_facts'::regclass AND contype='c'
                     AND pg_get_constraintdef(oid) LIKE '%elder_stated%' LOOP
            EXECUTE format('ALTER TABLE personal_facts DROP CONSTRAINT %I', r.conname);
          END LOOP;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE personal_facts ADD CONSTRAINT personal_facts_source_check "
        "CHECK (source IN ('operator', 'contact_stated', 'extracted'))"
    )
    op.execute("ALTER TABLE personal_facts ALTER COLUMN source SET DEFAULT 'contact_stated'")


def downgrade() -> None:
    op.execute("UPDATE personal_facts SET source='elder_stated' WHERE source='contact_stated'")
    op.execute(
        """
        DO $$ DECLARE r record; BEGIN
          FOR r IN SELECT conname FROM pg_constraint
                   WHERE conrelid='personal_facts'::regclass AND contype='c'
                     AND pg_get_constraintdef(oid) LIKE '%contact_stated%' LOOP
            EXECUTE format('ALTER TABLE personal_facts DROP CONSTRAINT %I', r.conname);
          END LOOP;
        END $$;
        """
    )
    op.execute(
        "ALTER TABLE personal_facts ADD CONSTRAINT personal_facts_source_check "
        "CHECK (source IN ('operator', 'elder_stated', 'extracted'))"
    )
    op.execute("ALTER TABLE personal_facts ALTER COLUMN source SET DEFAULT 'elder_stated'")
    op.execute(_RENAME_CONTACT_ID_TO_ELDER_ID)
    op.execute("ALTER TABLE contacts RENAME TO elders")
