#!/usr/bin/env bash
# Backup on one host, restore on a CLEAN one (docs/operations.md). Both are new disposable demo hosts of the committed
# source (quickstart-acceptance-ci.sh --demo artifact --show: the accepted draft, no model). Host A is taken to a full
# state (approved, deployed, run, dashboard + viewer), backed up (encrypted, the passphrase only in this process), and
# run once more AFTER the backup. Host B gets the same release; the operator's environment file (its secret store) and
# the archive are copied in; `dpagent backup restore`; B must then equal A at backup time, serve the viewer, and run.
#   bash scripts/ops-acceptance-ci.sh [--keep]
# Evidence: docs/evidence/ops/<sha>/<utc>-backup-restore/ (a/, b/, the manifest - never the archive or the env file).
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
KEEP=0; [ "${1:-}" = --keep ] && KEEP=1
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all -- . ':!docs/evidence')" \
  || { echo 'Commit source first: evidence must name a commit' >&2; exit 2; }
SHA=$(git -C "$ROOT" rev-parse HEAD)
OUT=${OPS_RESULTS_DIR:-$ROOT/docs/evidence/ops/$SHA/$(date -u +%Y%m%dT%H%M%SZ)-backup-restore}
mkdir -p "$OUT"; git -C "$ROOT" rev-parse HEAD 'HEAD^{tree}' > "$OUT/provenance.txt"
SECRET=$(mktemp -d); chmod 700 "$SECRET"
export OPS_BACKUP_PASSPHRASE="$(openssl rand -hex 24)"
export DEMO_ARTIFACT=docs/evidence/demo/4fce834eb1c1582bb405121038cdf7b9eec6f5fa/20261023T052117Z-ai \
  DEMO_ARTIFACT_TRACE_SHA256=2cbfa2905235ee0f2f7af91fea2fff25f800fca54cb679d0a1128ba28e1b1bd8
HOSTS=()
finish() {
  rc=$?; trap - EXIT; echo "$rc" > "$OUT/exit-code.txt"
  for h in "${HOSTS[@]}"; do
    docker cp "$h:/root/ops-evidence/." "$OUT/${h##*-ops-}/" 2>/dev/null || true
    if [ "$KEEP" = 0 ]; then docker rm -f "$h" >/dev/null; fi
  done
  # no secret of the environment file and no passphrase in the evidence
  if [ -f "$SECRET/demo-dev.env" ]; then
    grep -hE '^[A-Z_]*(PASSWORD|SECRET)[A-Z_]*=' "$SECRET/demo-dev.env" | cut -d= -f2- | while read -r v; do
      [ ${#v} -lt 8 ] || ! grep -rqF -- "$v" "$OUT" || { echo "a secret value is in the evidence - removed" >&2; rm -rf "$OUT"; exit 99; }
    done
  fi
  ! grep -rqF -- "$OPS_BACKUP_PASSPHRASE" "$OUT" || { echo "the passphrase is in the evidence - removed" >&2; rm -rf "$OUT"; exit 99; }
  rm -rf "$SECRET"
  exit "$rc"
}
trap finish EXIT

build() {   # build LABEL -> echoes the kept host's name
  mkdir -p "$OUT/host-$1"
  env -u GEMINI_API_KEY -u DPAGENT_MODEL QUICKSTART_RESULTS_DIR="$OUT/host-$1" \
    bash "$ROOT/scripts/quickstart-acceptance-ci.sh" --demo artifact --show > "$OUT/host-$1/build.log" 2>&1 \
    || { tail -20 "$OUT/host-$1/build.log" >&2; echo "host $1 did not build" >&2; return 3; }
  local name; name=$(sed -n 's/^live-demo host: \([^ ]*\).*/\1/p' "$OUT/host-$1/show-host.txt")
  docker rename "$name" "dpagent-ops-$1" && echo "dpagent-ops-$1"
}
X() { local h=$1; shift; docker exec -e OPS_BACKUP_PASSPHRASE "$h" bash -lc "bash /opt/dpagent/tests/ops_acceptance/inside.sh $*" \
        2>&1 | tee -a "$OUT/run-${h##*-ops-}.log"; return "${PIPESTATUS[0]}"; }

A=$(build a); HOSTS+=("$A")
X "$A" source
X "$A" backup
docker cp "$A:$(docker exec "$A" cat /root/ops-evidence/archive-name.txt)" "$SECRET/"
docker cp "$A:$(docker exec "$A" cat /root/ops-evidence/archive-name.txt).manifest.json" "$OUT/backup-manifest.json"
docker cp "$A:/root/demo-dev.env" "$SECRET/demo-dev.env"
X "$A" later
mkdir -p "$SECRET/src"; docker cp "$A:/root/ops-evidence/state-at-backup.txt" "$SECRET/src/"
docker cp "$A:/root/ops-evidence/state-after-later-run.txt" "$SECRET/src/"
[ "$KEEP" = 1 ] || { docker cp "$A:/root/ops-evidence/." "$OUT/a/"; docker rm -f "$A" >/dev/null; HOSTS=(); }

B=$(build b); HOSTS+=("$B")
X "$B" target
docker exec "$B" mkdir -p /root/backup /root/ops-src
docker cp "$SECRET/$(ls "$SECRET" | grep '\.enc$')" "$B:/root/backup/"
docker cp "$SECRET/src/." "$B:/root/ops-src/"
docker cp "$SECRET/demo-dev.env" "$B:/root/demo-dev.env"     # the operator's secret store, as kept outside the host
docker exec "$B" chmod 600 /root/demo-dev.env
X "$B" restore
echo "OPS BACKUP/RESTORE PASS" | tee "$OUT/result.txt"
