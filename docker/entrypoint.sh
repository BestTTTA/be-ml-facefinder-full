#!/bin/sh
# Runs DB migrations (idempotent) and then the API. Set SKIP_MIGRATIONS=1 to skip.
set -e
if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  echo "[entrypoint] running alembic upgrade head"
  alembic upgrade head
fi
exec "$@"
