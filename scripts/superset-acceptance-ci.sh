#!/usr/bin/env bash
# Generic Superset lifecycle acceptance on a new disposable host. No paid model calls.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
KEEP=0
case "${1:-}" in --keep) KEEP=1 ;; '') ;; *) echo 'usage: superset-acceptance-ci.sh [--keep]' >&2; exit 2 ;; esac
command -v docker >/dev/null
docker info >/dev/null
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all -- . ':!docs/evidence')" || { echo 'Commit source first' >&2; exit 2; }
SHA=$(git -C "$ROOT" rev-parse HEAD)
OUT=${SUPERSET_RESULTS_DIR:-$ROOT/docs/evidence/superset-pack/$SHA/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT"
git -C "$ROOT" rev-parse HEAD 'HEAD^{tree}' > "$OUT/provenance.txt"
git -C "$ROOT" status --porcelain=v1 > "$OUT/working-tree-before.txt"
NAME="dpagent-superset-acceptance-$$"
IMAGE=dpagent-m25-disposable
docker build -t "$IMAGE" -f "$ROOT/scripts/m25-disposable-host.Dockerfile" "$ROOT"
CID=''
cleanup() {
  rc=$?
  trap - EXIT
  echo "$rc" > "$OUT/exit-code.txt"
  if [ -n "$CID" ]; then
    docker cp "$NAME:/root/superset-acceptance/." "$OUT/" 2>/dev/null || true
    if [ "$KEEP" -eq 0 ]; then
      # Namespaced resource plus full ID: teardown cannot select existing containers.
      test "$(docker inspect -f '{{.Id}}' "$NAME")" = "$CID"
      docker rm -f "$CID" > "$OUT/container-teardown.txt"
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT
CID=$(docker run -d --name "$NAME" --privileged --cgroupns host --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "$ROOT:/opt/src/dpagent-src:ro" "$IMAGE")
echo "$NAME" > "$OUT/container-name.txt"
echo "$CID" > "$OUT/container-id.txt"
for i in $(seq 1 30); do
 state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)
 [ "$state" = running ] && break
 sleep 1
done
[ "$state" = running ] || { echo 'systemd probe failed' >&2; exit 2; }
docker exec -i "$NAME" bash -s <<'HOST'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl sudo ca-certificates >/dev/null
export DPAGENT_SOURCE_DIR=/opt/src/dpagent-src
bash /opt/src/dpagent-src/scripts/bootstrap.sh
mkdir -p /root/superset-acceptance
export WAREHOUSE_WRITER_PASSWORD=$(openssl rand -hex 20)
export SUPERSET_METADATA_PASSWORD=$(openssl rand -hex 20)
export SUPERSET_SECRET_KEY=$(openssl rand -hex 32)
export SUPERSET_ADMIN_PASSWORD=$(openssl rand -hex 20)
export SUPERSET_WAREHOUSE_PASSWORD=$(openssl rand -hex 20)
export DP_TEST_NS=dpagent_selftest_$(date -u +%s)
dpagent spec /opt/dpagent/tests/superset_acceptance/stack.yaml --yes --allow-draft
/opt/superset/.venv/bin/pip freeze > /root/superset-acceptance/resolved-requirements.txt
/opt/superset/.venv/bin/python /opt/dpagent/tests/superset_acceptance/lifecycle.py begin
dpagent spec /opt/dpagent/tests/superset_acceptance/stack.yaml --yes --allow-draft --force
/opt/superset/.venv/bin/python /opt/dpagent/tests/superset_acceptance/lifecycle.py after-reinstall
/opt/superset/.venv/bin/python /opt/dpagent/tests/superset_acceptance/lifecycle.py rollback
echo 'install=pass authenticated_query=pass api_and_db_write_denial=pass dbt_recreate=pass reinstall=pass restart=pass rollback=pass' > /root/superset-acceptance/lifecycle.txt
HOST
