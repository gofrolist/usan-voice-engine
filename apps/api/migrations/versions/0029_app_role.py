"""usan_app: non-superuser app login role subject to RLS

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-16

The app connects as this role so Row-Level Security (added in 0031/0032) actually
applies — superusers and BYPASSRLS roles ignore RLS. No password here (committed
migrations carry no secrets); the login password is provisioned out-of-band by
Terraform in prod (google_sql_user.usan_app) and by the test harness in CI.
ALTER DEFAULT PRIVILEGES (FOR ROLE usan, the migration runner/owner) ensures
tables created by later migrations are auto-granted to usan_app.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'usan_app') THEN
                CREATE ROLE usan_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT CONNECT ON DATABASE usan TO usan_app")
    op.execute("GRANT USAGE ON SCHEMA public TO usan_app")
    # CRUD on all current tables + sequence usage (serial PKs).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO usan_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO usan_app")
    # Future tables/sequences created by the migration runner (role usan) auto-granted.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE usan IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO usan_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE usan IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO usan_app"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE usan IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM usan_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE usan IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM usan_app"
    )
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM usan_app")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM usan_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM usan_app")
    op.execute("REVOKE CONNECT ON DATABASE usan FROM usan_app")
    op.execute("DROP ROLE IF EXISTS usan_app")
