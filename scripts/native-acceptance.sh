#!/usr/bin/env bash
# Native acceptance on a REAL Oracle Linux 8.10 (or RHEL 8) host or VM - not a container (docs/native-ol8.md).
# Everything runs over SSH with the same product commands and the same inside harness as the container runs
# (tests/demo_acceptance/inside.sh, tests/m25_acceptance/run_matrix.py). The host must start CLEAN (a fresh install
# or a reverted snapshot) with SELinux Enforcing and firewalld running; the runner refuses otherwise and never
# changes either. Evidence: docs/evidence/native/<sha>/<utc>-<suite>-<label>/.
#
#   bash scripts/native-acceptance.sh --ssh USER@HOST --suite demo   [--label vm1] [--identity KEY]
#   bash scripts/native-acceptance.sh --ssh USER@HOST --suite bronze [--label vm1] [--identity KEY]
#
# demo:   bootstrap, demo.sh install + lock, the accepted AI draft (--demo artifact: no model, no key), approval
#         stand-in, deploy/run = expected, schedule, dashboard + viewer (API, and a real browser through an SSH
#         tunnel), then REBOOT: services back, journal/Airflow/Superset metadata intact, the dashboard still serves the
#         same rows, a new run = expected; then bad data blocked, recovery, undeploy audit; then a REINSTALL
#         (install + lock again: idempotent); then SELinux denials, firewall and listening ports, system python.
# bronze: bootstrap, the bronze stack (examples/layer2-bronze-stack.yaml, seaweedfs pack), the bronze matrix
#         scenarios (run on a host reverted to the clean snapshot, not after the demo suite).
# USER needs passwordless sudo on the host (sudo -n). Nothing is installed on this machine except what the
# browser check already uses (docker).
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TARGET=""; SUITE=""; LABEL=host; IDENTITY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh) TARGET="${2:?}"; shift 2 ;;
    --suite) SUITE="${2:?}"; shift 2 ;;
    --label) LABEL="${2:?}"; shift 2 ;;
    --identity) IDENTITY="${2:?}"; shift 2 ;;
    *) echo "usage: native-acceptance.sh --ssh USER@HOST --suite demo|bronze [--label L] [--identity KEY]" >&2; exit 2 ;;
  esac
done
[ -n "$TARGET" ] || { echo "--ssh USER@HOST is required" >&2; exit 2; }
case "$SUITE" in demo|bronze) ;; *) echo "--suite must be demo or bronze" >&2; exit 2 ;; esac
case "$LABEL" in *[!A-Za-z0-9._-]*|'') echo "--label: letters, digits, . _ - only" >&2; exit 2 ;; esac
test -z "$(git -C "$ROOT" status --porcelain --untracked-files=all -- . ':!docs/evidence')" \
  || { echo 'Commit source first: evidence must name a commit' >&2; exit 2; }
SHA=$(git -C "$ROOT" rev-parse HEAD)
OUT=${NATIVE_RESULTS_DIR:-$ROOT/docs/evidence/native/$SHA/$(date -u +%Y%m%dT%H%M%SZ)-$SUITE-$LABEL}
mkdir -p "$OUT"
git -C "$ROOT" rev-parse HEAD 'HEAD^{tree}' > "$OUT/provenance.txt"
SSHO=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new)
[ -z "$IDENTITY" ] || SSHO+=(-i "$IDENTITY")
R() { ssh "${SSHO[@]}" "$TARGET" sudo -n bash -s; }          # R <<'EOF' ... EOF : a root script on the host
RL() { local label=$1; shift; echo "== $label" | tee -a "$OUT/steps.log"
       set +e; R > "$OUT/$label.log" 2>&1; local rc=$?; set -e
       echo "$label rc=$rc" | tee -a "$OUT/steps.log"; return $rc; }
trap 'rc=$?; echo "$rc" > "$OUT/exit-code.txt"; exit $rc' EXIT

# ---- 1. preflight: a real EL8 host, SELinux enforcing, firewalld running, resources, sudo - refuse otherwise
RL 00-preflight <<'EOF' || { cat "$OUT/00-preflight.log"; exit 4; }
set -u
. /etc/os-release; echo "os: $PRETTY_NAME ($ID $VERSION_ID)"; echo "kernel: $(uname -r)"
virt=$(systemd-detect-virt 2>/dev/null || true); echo "virtualization: ${virt:-none}"
case "$virt" in docker|podman|lxc|lxc-libvirt|systemd-nspawn|container-other) echo "REFUSED: a container is not a native host"; exit 1 ;; esac
case "$ID:$VERSION_ID" in ol:8.*|rhel:8.*|rocky:8.*|almalinux:8.*) ;; *) echo "REFUSED: not an EL8 host"; exit 1 ;; esac
echo "selinux: $(getenforce)"; [ "$(getenforce)" = Enforcing ] || { echo "REFUSED: SELinux is not Enforcing (never turned off for a test)"; exit 1; }
echo "firewalld: $(systemctl is-active firewalld)"; systemctl is-active -q firewalld || { echo "REFUSED: firewalld is not running"; exit 1; }
echo "cpus: $(nproc)"; awk '/MemTotal|MemAvailable/ {printf "%s %.1f GB\n", $1, $2/1048576}' /proc/meminfo
df -BG --output=avail / | tail -1 | tr -d ' G' | { read -r g; echo "disk free /: ${g} GB"; [ "$g" -ge 30 ] || { echo "REFUSED: < 30 GB free"; exit 1; }; }
[ ! -e /opt/dpagent ] || { echo "REFUSED: /opt/dpagent exists - start from a clean host or a reverted snapshot"; exit 1; }
command -v ausearch >/dev/null || dnf -y -q install audit >/dev/null
date -u +%s > /root/native-start-epoch
firewall-cmd --list-all > /root/native-firewall-before.txt; ss -ltnH > /root/native-ports-before.txt
dnf -y -q install python3 python3.11 python3.11-pip tar >/dev/null
echo "$(readlink -f /usr/bin/python3) $(python3 --version 2>&1)" > /root/system-python-before.txt
rpm -V python3 > /root/system-python-rpmv-before.txt 2>&1 || true
echo "preflight ok"
EOF

# ---- 2. the committed source, then bootstrap (the same as the EL8 container run)
git -C "$ROOT" archive --format=tar HEAD | ssh "${SSHO[@]}" "$TARGET" \
  'sudo -n bash -c "rm -rf /opt/src/clean && mkdir -p /opt/src/clean && tar -C /opt/src/clean -xf -"'
RL 01-bootstrap <<'EOF'
set -euo pipefail
DPAGENT_PYTHON=/usr/bin/python3.11 DPAGENT_SOURCE_DIR=/opt/src/clean bash /opt/src/clean/scripts/bootstrap.sh
EOF

fetch() { ssh "${SSHO[@]}" "$TARGET" "sudo -n tar -C $1 -cf - ." | tar -C "$2" -xf -; }

if [ "$SUITE" = bronze ]; then
  RL 02-bronze-stack <<'EOF'
set -euo pipefail
for v in DBT_DB_PASSWORD AIRFLOW_DB_PASSWORD AIRFLOW_ADMIN_PASSWORD DLT_DB_PASSWORD; do export "$v=$(openssl rand -hex 16)"; done
export BRONZE_S3_ACCESS_KEY="$(openssl rand -hex 10)" BRONZE_S3_SECRET_KEY="$(openssl rand -hex 20)"
dpagent spec /opt/dpagent/examples/layer2-bronze-stack.yaml --yes --allow-draft
EOF
  RL 03-bronze-matrix <<EOF || true
cd /opt/dpagent && M25_ACCEPTANCE_COMMIT=$SHA .venv/bin/python tests/m25_acceptance/run_matrix.py --profile bronze
EOF
  mkdir -p "$OUT/m25-evidence"; fetch /root/m25-evidence "$OUT/m25-evidence" || true
else
  DEMO_ENV="DEMO_DRAFT=artifact DEMO_ARTIFACT=docs/evidence/demo/4fce834eb1c1582bb405121038cdf7b9eec6f5fa/20261023T052117Z-ai DEMO_ARTIFACT_TRACE_SHA256=2cbfa2905235ee0f2f7af91fea2fff25f800fca54cb679d0a1128ba28e1b1bd8"
  RL 02-demo-first <<EOF
set -euo pipefail; export $DEMO_ENV; bash /opt/dpagent/tests/demo_acceptance/inside.sh first
EOF
  # the viewer in a real browser, through an SSH tunnel to the host's loopback-only Superset
  PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
  ssh "${SSHO[@]}" -f -N -o ExitOnForwardFailure=yes -L "127.0.0.1:$PORT:127.0.0.1:8088" "$TARGET"
  TUNNEL=$(pgrep -f "127.0.0.1:$PORT:127.0.0.1:8088" | head -1)
  RUNDIR=$(mktemp -d); chmod 700 "$RUNDIR"
  R > "$RUNDIR/viewer-auth.json" <<'EOF'
set -a; . /root/demo-dev.env; set +a; printf '{"username": "fulfil.viewer", "password": "%s"}' "$FS_VIEWER_PASSWORD"
EOF
  R > "$RUNDIR/viewer-targets.json" <<'EOF'
/opt/superset/.venv/bin/python - <<'PY'
import json, subprocess, yaml
from pathlib import Path
key = yaml.safe_load(Path("/opt/dpagent/projects/fulfil-sla/bundle/dashboards/overview.yaml").read_text())["uuid"]
did = subprocess.check_output(["runuser", "-u", "postgres", "--", "psql", "-X", "-At", "-d", "superset_meta", "-c",
                               f"SELECT id FROM dashboards WHERE uuid = '{key}'"], cwd="/", text=True).strip()
check = yaml.safe_load(Path("/opt/dpagent/projects/fulfil-sla/expected.yaml").read_text())["checks"][0]
f = check["filters"][0]
print(json.dumps({"own": {"url": f"/superset/dashboard/{did}/", "rows": check["rows"],
                          "filter_values": [str(v) for v in f["values"]], "filtered_rows": f["rows"]}}, default=str))
PY
EOF
  docker build -q -t dpagent-quickstart-browser -f "$ROOT/tests/data_platform_acceptance/browser.Dockerfile" \
    "$ROOT/tests/data_platform_acceptance" > "$OUT/browser-image.txt"
  mkdir -p "$OUT/browser"; set +e
  docker run --rm --network host -e "VIEWER_BASE=http://127.0.0.1:$PORT" -v "$RUNDIR:/run:ro" -v "$OUT/browser:/output" \
    -v "$ROOT/tests/quickstart_acceptance/viewer_browser.py:/viewer_browser.py:ro" \
    dpagent-quickstart-browser python /viewer_browser.py > "$OUT/browser/run.log" 2>&1
  BROWSER_RC=$?; set -e
  kill "$TUNNEL" 2>/dev/null || true
  if grep -rqF -- "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["password"])' "$RUNDIR/viewer-auth.json")" "$OUT/browser"; then
    rm -rf "$RUNDIR" "$OUT/browser"; echo "viewer password found in browser evidence - removed" >&2; exit 99
  fi
  rm -rf "$RUNDIR"; echo "viewer browser rc=$BROWSER_RC" | tee -a "$OUT/steps.log"
  [ "$BROWSER_RC" -eq 0 ] || exit 1

  # ---- reboot: what must survive it
  RL 03-before-reboot <<'EOF'
set -euo pipefail
cd /opt/dpagent
sqlite3 /var/lib/dpagent/dpagent.db 'SELECT count(*) FROM runs' > /root/native-runs-before.txt 2>/dev/null \
  || .venv/bin/python -c 'import sqlite3; print(sqlite3.connect("file:/var/lib/dpagent/dpagent.db?mode=ro", uri=True).execute("SELECT count(*) FROM runs").fetchone()[0])' > /root/native-runs-before.txt
runuser -u postgres -- psql -X -At -d superset_meta -c "SELECT count(*) FROM dashboards" > /root/native-dashboards-before.txt
runuser -u postgres -- psql -X -At -d airflow_meta -c "SELECT count(*) FROM dag_run" > /root/native-dagruns-before.txt
systemctl list-units --type=service --state=running --no-legend | awk '{print $1}' | sort > /root/native-services-before.txt
cat /root/native-runs-before.txt /root/native-dashboards-before.txt /root/native-dagruns-before.txt
EOF
  echo "== 04-reboot" | tee -a "$OUT/steps.log"
  ssh "${SSHO[@]}" "$TARGET" 'sudo -n systemctl reboot' || true
  sleep 30
  for _ in $(seq 1 60); do ssh "${SSHO[@]}" "$TARGET" true 2>/dev/null && break; sleep 10; done
  RL 05-after-reboot <<'EOF'
set -euo pipefail
cd /opt/dpagent
for _ in $(seq 1 60); do [ "$(systemctl is-system-running 2>/dev/null)" = running ] && break; sleep 5; done
echo "system: $(systemctl is-system-running || true)"
systemctl list-units --type=service --state=running --no-legend | awk '{print $1}' | sort > /root/native-services-after.txt
missing=$(comm -23 /root/native-services-before.txt /root/native-services-after.txt)
[ -z "$missing" ] || { echo "services not back after reboot: $missing"; exit 1; }
echo "selinux after reboot: $(getenforce)"; [ "$(getenforce)" = Enforcing ]
systemctl is-active -q firewalld
n=$(.venv/bin/python -c 'import sqlite3; print(sqlite3.connect("file:/var/lib/dpagent/dpagent.db?mode=ro", uri=True).execute("SELECT count(*) FROM runs").fetchone()[0])')
[ "$n" = "$(cat /root/native-runs-before.txt)" ] || { echo "journal runs changed: $n"; exit 1; }
[ "$(runuser -u postgres -- psql -X -At -d superset_meta -c 'SELECT count(*) FROM dashboards')" = "$(cat /root/native-dashboards-before.txt)" ]
[ "$(runuser -u postgres -- psql -X -At -d airflow_meta -c 'SELECT count(*) FROM dag_run')" -ge "$(cat /root/native-dagruns-before.txt)" ]
set -a; . /root/demo-dev.env; set +a
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:8088/health >/dev/null 2>&1 && break; sleep 5; done
/opt/superset/.venv/bin/python tests/quickstart_acceptance/check_viewer.py fulfil.viewer FS_VIEWER_PASSWORD fulfil-sla - \
  > /root/quickstart-evidence/check-viewer-after-reboot.json
dpagent pipeline run fulfilment_sla --yes --wait --timeout 1800
.venv/bin/python examples/demo/check_fulfil.py tests/llm_verification/cases/fulfilment_sla/expected.yaml \
  > /root/quickstart-evidence/check-after-reboot-run.json
echo "reboot: services back, SELinux enforcing, firewalld active, journal/metadata intact, viewer = expected, new run = expected"
EOF
  RL 06-demo-second <<EOF
set -euo pipefail; export $DEMO_ENV; bash /opt/dpagent/tests/demo_acceptance/inside.sh second
EOF
  # ---- reinstall: the same install again must be a no-op that still verifies
  RL 07-reinstall <<'EOF'
set -euo pipefail
cd /opt/dpagent; set -a; . /root/demo-dev.env; set +a
bash examples/demo/demo.sh install
bash examples/demo/demo.sh lock
EOF
  mkdir -p "$OUT/inside"; fetch /root/quickstart-evidence "$OUT/inside" || true
fi

# ---- SELinux, firewall, ports, system python - always recorded, judged here
RL 90-os-audit <<'EOF' || true
set -u
start=$(cat /root/native-start-epoch)
echo "== AVC denials since the run started"
ausearch -m AVC,USER_AVC,SELINUX_ERR -ts "$(date -d "@$start" '+%m/%d/%Y %H:%M:%S')" -i 2>/dev/null > /root/native-avc.txt || true
grep -c 'type=AVC' /root/native-avc.txt || true
echo "== firewall (before / after)"; firewall-cmd --list-all > /root/native-firewall-after.txt
diff /root/native-firewall-before.txt /root/native-firewall-after.txt && echo "firewall unchanged"
echo "== listening ports not on loopback"; ss -ltnpH | awk '$4 !~ /^(127\.|\[::1\])/'
echo "== system python"; echo "$(readlink -f /usr/bin/python3) $(python3 --version 2>&1)" > /root/system-python-after.txt
rpm -V python3 > /root/system-python-rpmv-after.txt 2>&1 || true
diff /root/system-python-before.txt /root/system-python-after.txt && diff /root/system-python-rpmv-before.txt /root/system-python-rpmv-after.txt && echo "system python unchanged"
echo "selinux: $(getenforce)  firewalld: $(systemctl is-active firewalld)"
EOF
ssh "${SSHO[@]}" "$TARGET" 'sudo -n cat /root/native-avc.txt' > "$OUT/avc-denials.txt" 2>/dev/null || true
AVC=$(grep -c 'type=AVC' "$OUT/avc-denials.txt" || true)
{ grep -q "firewall unchanged" "$OUT/90-os-audit.log" && grep -q "system python unchanged" "$OUT/90-os-audit.log" \
  && grep -q "selinux: Enforcing  firewalld: active" "$OUT/90-os-audit.log"; } \
  || { echo "OS audit failed: see 90-os-audit.log" | tee -a "$OUT/steps.log"; exit 1; }
if [ "${AVC:-0}" -gt 0 ]; then
  echo "AVC denials: $AVC - each must be explained before this run counts (avc-denials.txt)" | tee -a "$OUT/steps.log"; exit 6
fi
[ "$SUITE" = demo ] || awk '/requested scenarios matched/ {split($1, a, "/"); ok = (a[1] == a[2] && a[1] > 0)} END {exit !ok}' "$OUT/03-bronze-matrix.log" || { echo "bronze matrix not all matched" >&2; exit 1; }
echo "NATIVE $SUITE PASS on $(grep '^os:' "$OUT/00-preflight.log")" | tee -a "$OUT/steps.log"
