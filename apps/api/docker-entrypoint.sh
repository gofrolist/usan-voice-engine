#!/bin/sh
set -e

# Apply DB migrations on startup. In DEV/local the connecting user owns the schema, so this
# does the real work. In PROD the connecting user is the least-privilege `usan_app`
# (RLS-subject, NOT the table owner), which cannot run owner-level DDL — so the deploy
# pipeline runs migrations as the `usan` OWNER in a transient step BEFORE this container
# starts (see .github/workflows/build.yml), making this `upgrade head` a no-op there.
echo "Running database migrations..."
alembic upgrade head

echo "Starting API server..."
exec uvicorn usan_api.main:create_app --factory --host 0.0.0.0 --port 8000
