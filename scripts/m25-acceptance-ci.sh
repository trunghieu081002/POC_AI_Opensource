#!/usr/bin/env bash
# Step 3 of the plan docs/m25-acceptance.md was written under: builds a
# fresh disposable host, installs the real stack on it, runs the M2.5
# acceptance-matrix driver (tests/m25_acceptance/run_matrix.py) for real,
# and saves redacted evidence + a registry entry under docs/evidence/m25-automated/
# - every time this is re-run, not once by hand on a VM someone later
# deletes (docs/m25-vm-results.md's own stated gap: "Raw evidence
# currently resides on the VM, not in this repository").
#
#   bash scripts/m25-acceptance-ci.sh                   # build+run+teardown
#   bash scripts/m25-acceptance-ci.sh --keep             # leave the container up on exit (debugging)
#   bash scripts/m25-acceptance-ci.sh --scenarios "literal-connection timeout"
#
# What this does NOT do: the 6 scenarios in
# tests/m25_acceptance/run_matrix.py's own UNIMPLEMENTED_SCENARIOS (seed
# failure, partial/late deploy failure, source/warehouse provisioning
# failure, a real DROP failure) - those still need to be run by hand,
# same as they were on the original M2.5 VM, until someone scripts the
# host-level fault injection each one needs. This script says so itself
# (below) rather than let a clean exit code imply full coverage.
#
# Every precondition is checked and named before anything is built -
# "tự phát hiện thiếu infra và báo rõ, không giả định có sẵn" (M2.5 Step 3
# instruction this script was written against): no docker, no privileged-
# container support, no network to pull a base image, all fail loud here,
# never silently skip to "probably fine."
set -euo pipefail

KEEP=0
SCENARIOS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP=1; shift ;;
    --scenarios) SCENARIOS="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[36m::\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[32mok\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mXX\033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE="dpagent-m25-disposable"
CONTAINER="dpagent-m25-acceptance-$$"
RESULTS_DIR="${REPO_ROOT}/docs/evidence/m25-automated"

# ---------------------------------------------------------- preconditions

command -v docker >/dev/null 2>&1 || die "docker not found - install it, or run the matrix by hand per docs/m25-acceptance.md"
docker info >/dev/null 2>&1 || die "docker is installed but not usable by $(id -un) (needs the docker group, or root) - 'docker info' failed"

if ! grep -q cgroup /proc/filesystems 2>/dev/null; then
  die "this kernel has no cgroup support - a systemd-enabled container cannot run here (docs/deploy.md FAQ)"
fi

COMMIT="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || true)"
[ -n "$COMMIT" ] || die "could not resolve the current commit (git rev-parse HEAD failed) - run this from inside the repo's git checkout, not an extracted tarball with .git stripped"

say "preconditions ok - commit ${COMMIT}, building/reusing image ${IMAGE}"

# ---------------------------------------------------------------- build

docker build -q -t "${IMAGE}" -f "${SCRIPT_DIR}/m25-disposable-host.Dockerfile" "${REPO_ROOT}" \
  || die "image build failed - see output above"
ok "image ${IMAGE} ready"

cleanup() {
  if [ "$KEEP" -eq 1 ]; then
    say "--keep given: leaving ${CONTAINER} running for inspection (docker rm -f ${CONTAINER} when done)"
  else
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# -------------------------------------------------------------- launch

docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
docker run -d --name "${CONTAINER}" \
  --privileged \
  --tmpfs /run --tmpfs /run/lock \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v "${REPO_ROOT}:/opt/src/dpagent-src:ro" \
  "${IMAGE}" >/dev/null
# systemd needs a moment before it will answer is-system-running at all.
for _ in $(seq 1 30); do
  state="$(docker exec "${CONTAINER}" systemctl is-system-running 2>/dev/null || true)"
  [ "$state" = "running" ] && break
  sleep 1
done
[ "$state" = "running" ] || die "systemd never reached 'running' inside the container (got: ${state:-<none>}) - 'docker logs ${CONTAINER}' for why"
ok "container ${CONTAINER} up, systemd running"

# ------------------------------------------------------- bootstrap + install

say "bootstrapping dpagent and installing postgres+dlt+dbt+airflow (this takes several minutes)"
docker exec "${CONTAINER}" bash -lc '
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq sudo ca-certificates curl python3 python3-venv python3-pip >/dev/null
  export DPAGENT_SOURCE_DIR=/opt/src/dpagent-src
  bash /opt/src/dpagent-src/scripts/bootstrap.sh
  export DBT_DB_PASSWORD="$(openssl rand -hex 16)"
  export AIRFLOW_DB_PASSWORD="$(openssl rand -hex 16)"
  export AIRFLOW_ADMIN_PASSWORD="$(openssl rand -hex 16)"
  export DLT_DB_PASSWORD="$(openssl rand -hex 16)"
  dpagent spec /opt/dpagent/examples/layer2-stack.yaml --yes
' || die "bootstrap/install failed - see output above; nothing in this repo was touched, only the now-discarded container"

# ----------------------------------------------------------- re-verify

docker exec "${CONTAINER}" pg_isready >/dev/null 2>&1 || die "postgres installed but pg_isready failed post-install"
for svc in postgresql airflow-scheduler airflow-webserver; do
  state="$(docker exec "${CONTAINER}" systemctl is-active "$svc" 2>/dev/null || true)"
  [ "$state" = "active" ] || die "$svc is not active post-install (got: ${state:-<none>})"
done
docker exec "${CONTAINER}" bash -c 'sudo -n -u postgres true' \
  || die "passwordless sudo to postgres is not working inside the container - should be automatic for root via pam_rootok; something about this image changed that"
ok "postgres+dlt+dbt+airflow installed and verified running"

# -------------------------------------------------------------- run matrix

docker exec "${CONTAINER}" mkdir -p /opt/dpagent/tests/m25_acceptance
docker cp "${REPO_ROOT}/tests/m25_acceptance/registry.py" "${CONTAINER}:/opt/dpagent/tests/m25_acceptance/registry.py"
docker cp "${REPO_ROOT}/tests/m25_acceptance/run_matrix.py" "${CONTAINER}:/opt/dpagent/tests/m25_acceptance/run_matrix.py"

say "running the acceptance matrix driver"
set +e
docker exec -e "M25_ACCEPTANCE_COMMIT=${COMMIT}" "${CONTAINER}" \
  bash -c "cd /opt/dpagent && .venv/bin/python tests/m25_acceptance/run_matrix.py ${SCENARIOS}"
DRIVER_RC=$?
set -e

# --------------------------------------------------------- collect evidence

mkdir -p "${RESULTS_DIR}"
docker exec "${CONTAINER}" bash -c 'ls /root/m25-evidence/*.json 2>/dev/null' | while read -r remote_path; do
  name="$(basename "$remote_path")"
  docker cp "${CONTAINER}:${remote_path}" "/tmp/m25-raw-$$-${name}"
  python3 - "/tmp/m25-raw-$$-${name}" "${RESULTS_DIR}/${name}" "${REPO_ROOT}" <<'PYEOF'
import json, sys
raw_path, out_path, repo_root = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, f"{repo_root}/tests/m25_acceptance")
import registry
with open(raw_path) as f:
    data = json.load(f)
with open(out_path, "w") as f:
    json.dump(registry.redact_report(data), f, indent=2, sort_keys=True)
    f.write("\n")
PYEOF
  rm -f "/tmp/m25-raw-$$-${name}"
done
if docker exec "${CONTAINER}" test -f /root/m25-evidence/registry.jsonl; then
  docker cp "${CONTAINER}:/root/m25-evidence/registry.jsonl" "/tmp/m25-registry-$$.jsonl"
  cat "/tmp/m25-registry-$$.jsonl" >> "${RESULTS_DIR}/registry.jsonl"
  rm -f "/tmp/m25-registry-$$.jsonl"
fi
ok "evidence written under ${RESULTS_DIR}"

say "not automated by this script (run by hand per docs/m25-acceptance.md): seed-failure, partial-deploy-failure, late-deploy-failure, source-provisioning-failure, warehouse-provisioning-failure, drop-failure"

exit "$DRIVER_RC"
