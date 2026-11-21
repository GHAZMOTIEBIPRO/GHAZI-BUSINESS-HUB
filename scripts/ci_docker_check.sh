#!/usr/bin/env bash
# CI: build the production image and run it the way Render does (staging, PostgreSQL), then prove
#  1) it refuses to start without a PostgreSQL URL, 2) /healthz reports postgresql, 3) the live acceptance
#  checks pass against the container, 4) data written before a restart is still there after it.
# Needs: docker, a PostgreSQL reachable at $PG_URL from the host network.
set -euo pipefail
PG_URL=${PG_URL:?set PG_URL}
docker build -q -t ghazi-hub:ci . >/dev/null
echo "image built: $(docker image inspect ghazi-hub:ci --format '{{.Size}}' | awk '{printf "%.0f MB", $1/1048576}')"

set +e
timeout 60 docker run --rm -e HUB_ENV=staging -e HUB_SECRET_KEY=ci-docker-only ghazi-hub:ci >refuse.log 2>&1
rc=$?
set -e
if [ "$rc" = 0 ] || [ "$rc" = 124 ] || ! grep -q "needs PostgreSQL" refuse.log; then
  echo "FAIL: container started without a database URL (rc=$rc)"; tail -5 refuse.log; exit 1
fi
echo "PASS refuses to start on SQLite in staging (rc=$rc)"

run() {
  docker run -d --name hub --network host -e HUB_ENV=staging -e DATABASE_URL="$PG_URL" \
    -e HUB_SECRET_KEY=ci-docker-only -e HUB_JOBS_TOKEN=ci-docker-jobs -e PORT=8000 ghazi-hub:ci >/dev/null
  for i in $(seq 1 60); do curl -sf http://127.0.0.1:8000/healthz >/dev/null && return 0; sleep 1; done
  docker logs hub | tail -20; return 1
}
run
curl -s http://127.0.0.1:8000/healthz | tee health1.json; echo
grep -q '"db":"postgresql"' health1.json || { echo "FAIL: not on PostgreSQL"; exit 1; }
python scripts/live_check.py http://127.0.0.1:8000 --write
curl -sf -X POST -H "Authorization: Bearer ci-docker-jobs" http://127.0.0.1:8000/internal/jobs/refresh >/dev/null
echo "PASS refresh recorded; restarting the container"
docker restart hub >/dev/null
for i in $(seq 1 60); do curl -sf http://127.0.0.1:8000/healthz >health2.json && break; sleep 1; done
cat health2.json; echo
python -c "import json,sys; h=json.load(open('health2.json')); sys.exit(0 if h['last_refresh_at'] and h['last_refresh_at'] < h['started_at'] else 1)" \
  && echo "PASS data survived a restart (refresh stored before the new process started)" \
  || { echo "FAIL: data lost on restart"; exit 1; }
docker logs hub 2>&1 | grep -iE "traceback|error" | grep -v "GET /dl/not-a-real-token" | head -5 || true
docker rm -f hub >/dev/null
