#!/bin/sh
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Starting API server..."
exec uvicorn usan_api.main:create_app --factory --host 0.0.0.0 --port 8000
