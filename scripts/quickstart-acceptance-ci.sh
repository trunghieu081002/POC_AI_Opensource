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
docker exec "$NAME" bash -lc 'bash /opt/dpagent/tests/quickstart_acceptance/inside.sh first' 2>&1 | tee "$OUT/run-first.log"

# Viewer RBAC in a real browser, logged in as the sales viewer: a Playwright container sharing the host's network
# namespace (Superset binds loopback). The password goes through a 0600 file that is removed right after.
docker build -q -t dpagent-quickstart-browser -f "$ROOT/tests/data_platform_acceptance/browser.Dockerfile" \
  "$ROOT/tests/data_platform_acceptance" > "$OUT/browser-image.txt"
RUNDIR=$(mktemp -d); chmod 700 "$RUNDIR"
docker exec "$NAME" bash -c 'set -a; . /root/quickstart-dev.env; set +a
  printf "{\"username\": \"qs.sales.viewer\", \"password\": \"%s\"}" "$QS_SALES_VIEWER_PASSWORD"' > "$RUNDIR/viewer-auth.json"
chmod 600 "$RUNDIR/viewer-auth.json"
docker exec "$NAME" /opt/superset/.venv/bin/python - > "$RUNDIR/viewer-targets.json" <<'PY'
import json, subprocess, yaml
from pathlib import Path
def dash_id(project):
    key = yaml.safe_load(Path(f"/opt/dpagent/projects/{project}/bundle/dashboards/overview.yaml").read_text())["uuid"]
    return subprocess.check_output(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-d", "superset_meta",
                                    "-c", f"SELECT id FROM dashboards WHERE uuid = '{key}'"], cwd="/", text=True).strip()
check = yaml.safe_load(Path("/opt/dpagent/projects/quickstart-sales/expected.yaml").read_text())["checks"][0]
f = check["filters"][0]
print(json.dumps({"own": {"url": f"/superset/dashboard/{dash_id('quickstart-sales')}/", "rows": check["rows"],
                          "filter_values": [str(v) for v in f["values"]], "filtered_rows": f["rows"]},
                  "other": {"url": f"/superset/dashboard/{dash_id('quickstart-ops')}/"}}, default=str))
PY
mkdir -p "$OUT/browser"
set +e
docker run --rm --network "container:$NAME" -v "$RUNDIR:/run:ro" -v "$OUT/browser:/output" \
  -v "$ROOT/tests/quickstart_acceptance/viewer_browser.py:/viewer_browser.py:ro" \
  dpagent-quickstart-browser python /viewer_browser.py > "$OUT/browser/run.log" 2>&1
BROWSER_RC=$?
set -e
if grep -rqF -- "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["password"])' "$RUNDIR/viewer-auth.json")" "$OUT/browser"; then
  rm -rf "$RUNDIR" "$OUT/browser"; echo "viewer password found in browser evidence - removed" >&2; exit 99
fi
rm -rf "$RUNDIR"
echo "viewer browser rc=$BROWSER_RC" | tee -a "$OUT/browser/run.log"
[ "$BROWSER_RC" -eq 0 ] || { tail -30 "$OUT/browser/run.log"; exit 1; }

docker exec "$NAME" bash -lc 'bash /opt/dpagent/tests/quickstart_acceptance/inside.sh second' 2>&1 | tee "$OUT/run-second.log"
