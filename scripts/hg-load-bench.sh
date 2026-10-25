#!/usr/bin/env bash
# HG-shaped SYNTHETIC load benchmark (benchmarks/hg_usage/README.md) - "synthetic, in HG's table shape; not HG's
# data, not an HG load acceptance". Builds a NEW bronze host with the M2.5 runner (examples/layer2-bronze-stack.yaml,
# SeaweedFS pack; one fixture scenario proves the host before anything is measured), runs benchmarks/hg_usage/bench.py
# in it for the given scales one after another, copies the evidence out and removes the host.
#   bash scripts/hg-load-bench.sh [--keep] SCALE [SCALE ...]        e.g. 10000 100000 1000000
# Thresholds (passed to bench.py): HGBENCH_TIMEOUT (s, per run, default 3600), HGBENCH_MIN_FREE_GB (10),
# HGBENCH_MIN_AVAIL_GB (3). The Docker host's own free disk is checked before the build (>= 25 GB).
# Evidence: docs/evidence/hg-load/<sha>/<utc>/.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
KEEP=0; SCALES=()
for a in "$@"; do
  case "$a" in --keep) KEEP=1 ;; *[!0-9]*|'') echo "usage: hg-load-bench.sh [--keep] SCALE [SCALE ...]" >&2; exit 2 ;; *) SCALES+=("$a") ;; esac
done
[ "${#SCALES[@]}" -gt 0 ] || { echo "usage: hg-load-bench.sh [--keep] SCALE [SCALE ...]" >&2; exit 2; }
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all -- . ':!docs/evidence')" \
  || { echo 'Commit source first: evidence must name a commit' >&2; exit 2; }
SHA=$(git -C "$ROOT" rev-parse HEAD)
OUT=${HGBENCH_RESULTS_DIR:-$ROOT/docs/evidence/hg-load/$SHA/$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT/host"; git -C "$ROOT" rev-parse HEAD 'HEAD^{tree}' > "$OUT/provenance.txt"
free=$(df -BG --output=avail "$(docker info -f '{{.DockerRootDir}}')" | tail -1 | tr -dc 0-9)
{ echo "docker host free disk: ${free} GB"; uptime; free -g | sed -n 2p; nproc; } > "$OUT/host-preflight.txt"
[ "$free" -ge 25 ] || { cat "$OUT/host-preflight.txt"; echo "refused: < 25 GB free on the Docker host" >&2; exit 4; }
trap 'rc=$?; echo "$rc" > "$OUT/exit-code.txt"; exit $rc' EXIT

# ---- the host: the M2.5 runner, bronze profile, one bronze fixture scenario, kept
M25_RESULTS_DIR="$OUT/host" bash "$ROOT/scripts/m25-acceptance-ci.sh" --profile bronze --scenarios usage-correct-twice --keep \
  > "$OUT/host-build.log" 2>&1 &
RUNNER=$!
wait "$RUNNER" || { tail -30 "$OUT/host-build.log"; echo "the bronze host did not pass its own scenario" >&2; exit 3; }
NAME="dpagent-m25-acceptance-$RUNNER"
CID=$(docker inspect -f '{{.Id}}' "$NAME")
cleanup() {
  rc=$?; trap - EXIT; echo "$rc" > "$OUT/exit-code.txt"
  docker cp "$NAME:/root/hgbench-evidence/." "$OUT/" 2>/dev/null || true
  if [ "$KEEP" = 0 ]; then docker rm -f "$CID" > "$OUT/container-teardown.txt"; else echo "kept $NAME" > "$OUT/container-kept.txt"; fi
  exit "$rc"
}
trap cleanup EXIT
# the committed benchmark files (the host's /opt/dpagent is this commit's copy; be explicit about it)
git -C "$ROOT" archive --format=tar HEAD benchmarks/hg_usage tests/m25_acceptance pipelines/hg_resources_useage_number \
  | docker exec -i "$NAME" tar -C /opt/dpagent -xf -
docker exec "$NAME" bash -lc "cd /opt/dpagent && .venv/bin/python benchmarks/hg_usage/bench.py ${SCALES[*]} \
  --timeout ${HGBENCH_TIMEOUT:-3600} --min-free-gb ${HGBENCH_MIN_FREE_GB:-10} --min-avail-gb ${HGBENCH_MIN_AVAIL_GB:-3}" \
  2>&1 | tee "$OUT/bench.log"
exit "${PIPESTATUS[0]}"
