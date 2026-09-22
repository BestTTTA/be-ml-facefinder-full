#!/bin/sh
# Build the current checkout on this host and swap the stack over to it.
#
# The image is built here rather than pulled, because this host is arm64 while the
# CI runners are amd64. Building on the target also skips pushing ~3GB per release.
#
#   ./docker/deploy.sh [tag]        # tag defaults to the short commit SHA
#
# On a failed health check the previous image is brought back up, so a bad release
# costs a restart rather than an outage.
set -eu

cd "$(CDPATH= cd "$(dirname "$0")/.." && pwd)"

TAG="${1:-$(git rev-parse --short HEAD)}"
IMAGE="facefinder-api:${TAG}"
COMPOSE_FILE="docker-compose.prod.yml"
CONTAINER="be-facemenow"

if [ ! -f .env ]; then
  echo "deploy: .env is missing next to $COMPOSE_FILE" >&2
  exit 1
fi

API_PORT=$(sed -n 's/^API_PORT=//p' .env | tail -1)
API_PORT="${API_PORT:-7777}"

# Remember what is serving traffic now, so a failure has somewhere to fall back to.
PREV_IMAGE=$(docker inspect -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null || true)
echo "deploy: current image = ${PREV_IMAGE:-<none>}"

echo "deploy: building $IMAGE"
docker build -t "$IMAGE" .

echo "deploy: starting $IMAGE"
ok=0
# `up` itself can fail — an unhealthy dependency, say — and that has to reach the
# rollback below rather than aborting the script through set -e.
if DOCKER_IMAGE="$IMAGE" docker compose -f "$COMPOSE_FILE" up -d; then
  # The model load and the alembic upgrade both happen at startup, so give it time.
  echo "deploy: waiting for /health/ready on port $API_PORT"
  i=0
  while [ "$i" -lt 60 ]; do
    if curl -fsS "http://127.0.0.1:${API_PORT}/health/ready" >/dev/null 2>&1; then
      ok=1
      break
    fi
    if [ -z "$(docker ps -q -f "name=^${CONTAINER}$")" ]; then
      echo "deploy: container exited" >&2
      break
    fi
    i=$((i + 1))
    sleep 5
  done
else
  echo "deploy: compose up failed" >&2
fi

if [ "$ok" = "1" ]; then
  echo "deploy: healthy on $IMAGE"
  docker image prune -f >/dev/null 2>&1 || true
  exit 0
fi

echo "deploy: FAILED - last 60 log lines follow" >&2
docker logs --tail 60 "$CONTAINER" >&2 2>&1 || true

if [ -n "$PREV_IMAGE" ] && docker image inspect "$PREV_IMAGE" >/dev/null 2>&1; then
  echo "deploy: rolling back to $PREV_IMAGE" >&2
  DOCKER_IMAGE="$PREV_IMAGE" docker compose -f "$COMPOSE_FILE" up -d
  echo "deploy: rolled back" >&2
else
  echo "deploy: no previous image to roll back to" >&2
fi
exit 1
