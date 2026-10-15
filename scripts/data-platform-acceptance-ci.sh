#!/usr/bin/env bash
# One disposable pack-composed stack. No SSH configuration or host credentials.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=${PLATFORM_RESULTS_DIR:?set a fresh evidence directory}
IMAGE=${PLATFORM_IMAGE:-dpagent-m25-disposable}
NAME=dpagent-platform-acceptance-$(date -u +%s)
mkdir -p "$OUT"
exec 9>"${PLATFORM_LOCK:-/tmp/dpagent-platform-acceptance.lock}"
flock -n 9 || { echo 'another platform acceptance is running';exit 73; }
finish(){ rc=$?;echo "$rc" > "$OUT/exit-code.txt";date -u > "$OUT/finished.txt"; }
trap finish EXIT
git -C "$ROOT" rev-parse HEAD 'HEAD^{tree}' > "$OUT/source.txt"
git -C "$ROOT" status --porcelain=v1 --untracked-files=all > "$OUT/working-tree-before.txt"
test ! -s "$OUT/working-tree-before.txt" || { echo 'source checkout must be clean';exit 2; }
docker info >/dev/null
docker image inspect "$IMAGE" --format '{{.Id}}' > "$OUT/image.txt"
CID=$(docker run -d --name "$NAME" --hostname dpagent-platform-test --privileged --cgroupns host --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "$ROOT:/opt/src/dpagent-src:ro" "$IMAGE")
printf '%s\n' "$CID" > "$OUT/container-id.txt";printf '%s\n' "$NAME" > "$OUT/container-name.txt"
for i in $(seq 1 30);do
 state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)
 [ "$state" = running ] && break
 sleep 1
done
test "$state" = running
docker exec "$NAME" test ! -w /opt/src/dpagent-src/scripts/data-platform-acceptance-ci.sh
# Keep on failure for diagnosis; never retry bootstrap automatically.
set +e
docker exec -i "$NAME" bash -s > "$OUT/runner.log" 2>&1 <<'HOST'
set -euo pipefail
unset GEMINI_API_KEY GOOGLE_API_KEY GROQ_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY OPENROUTER_API_KEY DPAGENT_MODEL
export DEBIAN_FRONTEND=noninteractive DPAGENT_WITH_LLM= DPAGENT_SOURCE_DIR=/opt/src/dpagent-src
apt-get update -qq
apt-get install -y -qq sudo ca-certificates curl python3 python3-venv python3-pip >/dev/null
for url in https://pypi.org/simple/apache-superset/ https://raw.githubusercontent.com/apache/airflow/constraints-2.9.3/constraints-3.10.txt https://apt.postgresql.org/pub/repos/apt/dists/jammy-pgdg/Release;do curl -fsSL --max-time 25 -o /dev/null "$url";done
bash /opt/src/dpagent-src/scripts/bootstrap.sh
python3 - <<'PY'
import json,secrets
from pathlib import Path
keys=('DBT_DB_PASSWORD','AIRFLOW_DB_PASSWORD','AIRFLOW_ADMIN_PASSWORD','DLT_DB_PASSWORD','BRONZE_S3_ACCESS_KEY','BRONZE_S3_SECRET_KEY','SUPERSET_METADATA_PASSWORD','SUPERSET_SECRET_KEY','SUPERSET_ADMIN_PASSWORD','SUPERSET_WAREHOUSE_PASSWORD')
p=Path('/root/platform-test-secrets.json');p.write_text(json.dumps({k:secrets.token_hex(32) for k in keys}));p.chmod(0o600)
PY
while IFS= read -r -d '' entry;do export "$entry";done < <(python3 - <<'PY'
import json,sys
for key,value in json.load(open('/root/platform-test-secrets.json')).items():sys.stdout.buffer.write((key+'='+value).encode()+b'\0')
PY
)
cd /opt/dpagent
# Installation is proved by its component suites before composed data acceptance.
dpagent spec tests/data_platform_acceptance/stack-data.yaml --yes --allow-draft
.venv/bin/python tests/data_platform_acceptance/run.py
HOST
rc=$?
set -e
docker cp "$NAME:/root/data-platform-evidence" "$OUT/" 2>/dev/null || true
echo "$rc" > "$OUT/composed-exit-code.txt"
# Explicit follow-up audits/teardown after inspection; never claim clean here.
echo "retained_owned_container=$NAME" > "$OUT/retained-container.txt"
exit "$rc"
