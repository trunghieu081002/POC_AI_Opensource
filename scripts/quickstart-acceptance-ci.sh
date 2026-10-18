#!/usr/bin/env bash
# Quickstart acceptance (docs/quickstart.md) on a NEW disposable systemd host built from packs: a clean copy of
# the committed source, the operator steps of examples/quickstart/walkthrough.sh (product commands only), and
# independent checks after each milestone (tests/quickstart_acceptance/). No paid model call. Evidence lands in
# docs/evidence/quickstart/<sha>/<utc>/ (or $QUICKSTART_RESULTS_DIR); failed runs are kept too.
#   bash scripts/quickstart-acceptance-ci.sh [--keep]
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
KEEP=0
[ "${1:-}" != "--keep" ] || KEEP=1
command -v docker >/dev/null && docker info >/dev/null
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all -- . ':!docs/evidence')" \
  || { echo 'Commit source first: evidence must name a commit' >&2; exit 2; }
SHA=$(git -C "$ROOT" rev-parse HEAD)
OUT=${QUICKSTART_RESULTS_DIR:-$ROOT/docs/evidence/quickstart/$SHA/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT"
git -C "$ROOT" rev-parse HEAD 'HEAD^{tree}' > "$OUT/provenance.txt"
NAME="dpagent-quickstart-acceptance-$$"
IMAGE=dpagent-m25-disposable
docker build -q -t "$IMAGE" -f "$ROOT/scripts/m25-disposable-host.Dockerfile" "$ROOT" > "$OUT/image.txt"
CID=''
cleanup() {
  rc=$?; trap - EXIT
  echo "$rc" > "$OUT/exit-code.txt"
  if [ -n "$CID" ]; then
    docker cp "$NAME:/root/quickstart-evidence/." "$OUT/" 2>/dev/null || true
    if [ "$KEEP" -eq 0 ]; then
      test "$(docker inspect -f '{{.Id}}' "$NAME")" = "$CID" && docker rm -f "$CID" > "$OUT/container-teardown.txt"
    else
      echo "kept $NAME ($CID) for inspection: docker rm -f $CID" | tee "$OUT/container-kept.txt"
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT
CID=$(docker run -d --name "$NAME" --privileged --cgroupns host --tmpfs /run --tmpfs /run/lock \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw -v "$ROOT:/opt/src/dpagent-src:ro" "$IMAGE")
echo "$CID" > "$OUT/container-id.txt"
for _ in $(seq 1 30); do
  [ "$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)" = running ] && break; sleep 1
done
docker exec "$NAME" systemctl is-system-running
# A clean checkout: only committed files reach the host (git archive), never this working tree's extras.
git -C "$ROOT" archive --format=tar HEAD | docker exec -i "$NAME" bash -c 'mkdir -p /opt/src/clean && tar -C /opt/src/clean -xf -'
docker exec "$NAME" bash -lc '
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq sudo ca-certificates curl openssl python3 python3-venv python3-pip >/dev/null
  DPAGENT_SOURCE_DIR=/opt/src/clean bash /opt/src/clean/scripts/bootstrap.sh
' > "$OUT/bootstrap.log" 2>&1
docker exec "$NAME" bash -lc 'bash /opt/dpagent/tests/quickstart_acceptance/inside.sh' 2>&1 | tee "$OUT/run.log"
