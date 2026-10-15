#!/usr/bin/env bash
# Generic Superset lifecycle acceptance on a new disposable host. No paid model calls.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
KEEP=0
exec 9>"${SUPERSET_LOCK_FILE:-${TMPDIR:-/tmp}/dpagent-superset-acceptance-$(id -u).lock}"
flock -n 9 || { echo "Another Superset acceptance is running" >&2; exit 73; }
REUSE=""
while [ $# -gt 0 ]; do
 case "$1" in
  --keep) KEEP=1; shift ;;
  --reuse-container) [ $# -ge 2 ] || { echo 'Missing reuse name' >&2; exit 2; }; REUSE="$2"; shift 2 ;;
  *) echo 'usage: superset-acceptance-ci.sh [--keep] [--reuse-container NAME]' >&2; exit 2 ;;
 esac
done
if [ -n "$REUSE" ]; then
 case "$REUSE" in dpagent-superset-acceptance-*) ;; *) echo 'Refusing non-runner container' >&2; exit 2 ;; esac
fi
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
if [ -z "$REUSE" ]; then docker build -t "$IMAGE" -f "$ROOT/scripts/m25-disposable-host.Dockerfile" "$ROOT"; fi
(cd "$ROOT"; git ls-files -z src tests scripts packs suites examples pyproject.toml | xargs -0 sha256sum) > "$OUT/source-manifest.txt"
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
if [ -z "$REUSE" ]; then
 CID=$(docker run -d --name "$NAME" --privileged --cgroupns host --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "$ROOT:/opt/src/dpagent-src:ro" "$IMAGE")
else
 NAME="$REUSE"
 test "$(docker inspect -f '{{.Config.Image}}' "$NAME")" = "$IMAGE"
 docker inspect -f '{{range .Mounts}}{{if eq .Destination "/opt/src/dpagent-src"}}{{.Source}} {{.RW}}{{end}}{{end}}' "$NAME" | grep -Fxq "$ROOT false"
 docker exec "$NAME" test -f /root/superset-test-secrets.json
 CID=$(docker inspect -f '{{.Id}}' "$NAME")
 docker exec "$NAME" bash -c 'set -euo pipefail; tar -C /opt/src/dpagent-src --exclude=.git --exclude=.venv --exclude=docs/evidence --exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -C /opt/dpagent -xf -; if [ -d /root/superset-acceptance ]; then mv /root/superset-acceptance /root/superset-acceptance.previous-$(date -u +%s); fi'
fi
docker cp "$OUT/source-manifest.txt" "$NAME:/tmp/superset-source-manifest.txt"
echo "$NAME" > "$OUT/container-name.txt"
echo "$CID" > "$OUT/container-id.txt"
for i in $(seq 1 30); do
 state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)
 [ "$state" = running ] && break
 sleep 1
done
[ "$state" = running ] || { echo 'systemd probe failed' >&2; exit 2; }
docker exec -i -e SUPERSET_REUSE="${REUSE:+1}" "$NAME" bash -s <<'HOST'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if [ -z "$SUPERSET_REUSE" ]; then
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
python3 - <<'SECRETS'
import json,os
from pathlib import Path
p=Path("/root/superset-test-secrets.json")
p.write_text(json.dumps({k:v for k,v in os.environ.items() if k in ("SUPERSET_METADATA_PASSWORD","SUPERSET_SECRET_KEY","SUPERSET_ADMIN_PASSWORD","SUPERSET_WAREHOUSE_PASSWORD","WAREHOUSE_WRITER_PASSWORD")}))
p.chmod(0o600)
SECRETS
else
 while IFS= read -r -d '' entry; do export "$entry"; done < <(python3 - <<'RECOVER'
import json,sys
for key,value in json.load(open('/root/superset-test-secrets.json')).items():
 if key in ('SUPERSET_METADATA_PASSWORD','SUPERSET_SECRET_KEY','SUPERSET_ADMIN_PASSWORD','SUPERSET_WAREHOUSE_PASSWORD','WAREHOUSE_WRITER_PASSWORD'):sys.stdout.buffer.write((key+'='+value).encode()+b'\0')
RECOVER
 )
fi
mkdir -p /root/superset-acceptance
cd /opt/dpagent
sha256sum -c /tmp/superset-source-manifest.txt >/dev/null
export DP_TEST_NS=dpagent_selftest_$(date -u +%s)
if [ -n "$SUPERSET_REUSE" ]; then
 dpagent spec /opt/dpagent/tests/superset_acceptance/stack.yaml --yes --allow-draft --force --no-test
 dpagent test superset
else
 dpagent spec /opt/dpagent/tests/superset_acceptance/stack.yaml --yes --allow-draft
fi
/opt/superset/.venv/bin/pip freeze > /root/superset-acceptance/resolved-requirements.txt
/opt/superset/.venv/bin/python /opt/dpagent/tests/superset_acceptance/lifecycle.py begin
dpagent spec /opt/dpagent/tests/superset_acceptance/stack.yaml --yes --allow-draft --force
/opt/superset/.venv/bin/python /opt/dpagent/tests/superset_acceptance/lifecycle.py after-reinstall
/opt/superset/.venv/bin/python /opt/dpagent/tests/superset_acceptance/lifecycle.py rollback
echo 'install=pass authenticated_query=pass api_and_db_write_denial=pass dbt_recreate=pass reinstall=pass restart=pass rollback=pass' > /root/superset-acceptance/lifecycle.txt
HOST
