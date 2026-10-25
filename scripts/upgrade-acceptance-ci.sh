#!/usr/bin/env bash
# Upgrade from the previous release to this commit, and roll back (docs/operations.md). A NEW demo host is built from
# the PREVIOUS release (default v0.4.0-demo-rc1 = c3c1340, by that release's own runner; the accepted AI draft, no
# model), taken to a full state (approved, deployed, run, dashboard + viewer), backed up, upgraded in place to this
# commit (bootstrap + `dpagent spec` with the same stack), checked, then rolled back (old release + restore), checked.
#   bash scripts/upgrade-acceptance-ci.sh [--from COMMIT] [--keep]
# Evidence: docs/evidence/ops/<sha>/<utc>-upgrade-from-<old>/ (never the archive or the environment file).
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OLD=c3c1340; KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in --from) OLD="${2:?}"; shift 2 ;; --keep) KEEP=1; shift ;; *) echo "usage: [--from COMMIT] [--keep]" >&2; exit 2 ;; esac
done
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all -- . ':!docs/evidence')" \
  || { echo 'Commit source first: evidence must name a commit' >&2; exit 2; }
SHA=$(git -C "$ROOT" rev-parse HEAD); OLDSHA=$(git -C "$ROOT" rev-parse "$OLD")
OUT=${OPS_RESULTS_DIR:-$ROOT/docs/evidence/ops/$SHA/$(date -u +%Y%m%dT%H%M%SZ)-upgrade-from-${OLDSHA:0:7}}
mkdir -p "$OUT/host-old"; printf 'new=%s\nold=%s\n' "$SHA" "$OLDSHA" > "$OUT/provenance.txt"
WT=$(mktemp -d); git -C "$ROOT" worktree add -q --detach "$WT/old" "$OLDSHA"
SECRET=$(mktemp -d); chmod 700 "$SECRET"
export OPS_BACKUP_PASSPHRASE="$(openssl rand -hex 24)"
NAME=""
finish() {
  rc=$?; trap - EXIT; echo "$rc" > "$OUT/exit-code.txt"
  if [ -n "$NAME" ]; then
    docker cp "$NAME:/root/upgrade-evidence/." "$OUT/" 2>/dev/null || true
    docker cp "$NAME:/root/demo-dev.env" "$SECRET/env" 2>/dev/null || true
    [ "$KEEP" = 1 ] || docker rm -f "$NAME" >/dev/null
  fi
  if [ -f "$SECRET/env" ]; then
    grep -hE '^[A-Z_]*(PASSWORD|SECRET)[A-Z_]*=' "$SECRET/env" | cut -d= -f2- | while read -r v; do
      [ ${#v} -lt 8 ] || ! grep -rqF -- "$v" "$OUT" || { echo "a secret value is in the evidence - removed" >&2; rm -rf "$OUT"; exit 99; }
    done
  fi
  ! grep -rqF -- "$OPS_BACKUP_PASSPHRASE" "$OUT" || { rm -rf "$OUT"; exit 99; }
  git -C "$ROOT" worktree remove --force "$WT/old" 2>/dev/null || true
  rm -rf "$WT" "$SECRET"
  exit "$rc"
}
trap finish EXIT

# ---- the old release's host, by the old release's own runner
env -u GEMINI_API_KEY -u DPAGENT_MODEL QUICKSTART_RESULTS_DIR="$OUT/host-old" \
  DEMO_ARTIFACT=docs/evidence/demo/4fce834eb1c1582bb405121038cdf7b9eec6f5fa/20261023T052117Z-ai \
  DEMO_ARTIFACT_TRACE_SHA256=2cbfa2905235ee0f2f7af91fea2fff25f800fca54cb679d0a1128ba28e1b1bd8 \
  bash "$WT/old/scripts/quickstart-acceptance-ci.sh" --demo artifact --show > "$OUT/host-old/build.log" 2>&1 \
  || { tail -20 "$OUT/host-old/build.log"; echo "the old release's host did not build" >&2; exit 3; }
NAME=$(sed -n 's/^live-demo host: \([^ ]*\).*/\1/p' "$OUT/host-old/show-host.txt")
docker exec "$NAME" mkdir -p /opt/src/new /opt/src/old
git -C "$ROOT" archive --format=tar HEAD | docker exec -i "$NAME" tar -C /opt/src/new -xf -
git -C "$ROOT" archive --format=tar "$OLDSHA" | docker exec -i "$NAME" tar -C /opt/src/old -xf -
docker cp "$ROOT/tests/ops_acceptance/upgrade.sh" "$NAME:/root/upgrade.sh"
for phase in baseline backup upgrade rollback; do
  docker exec -e OPS_BACKUP_PASSPHRASE "$NAME" bash -lc "bash /root/upgrade.sh $phase" 2>&1 | tee -a "$OUT/run.log"
  [ "${PIPESTATUS[0]}" -eq 0 ] || exit 1
done
echo "UPGRADE ${OLDSHA:0:7} -> ${SHA:0:7} AND ROLLBACK PASS" | tee "$OUT/result.txt"
