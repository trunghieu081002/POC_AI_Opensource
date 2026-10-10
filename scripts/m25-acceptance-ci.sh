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
#   bash scripts/m25-acceptance-ci.sh --profile bronze   # + SeaweedFS (installed by its pack) and the
#                                                       # hg_dbt_branch scenarios: bronze_staging +
#                                                       # an owned dbt project, through the real
#                                                       # fixture-validation path
#
# All 12 scenarios in tests/m25_acceptance/run_matrix.py's own SCENARIOS
# are driven here. Each one's real outcome is checked against its own
# EXPECTATIONS entry by the driver itself - this script's exit code is
# only ever as good as that check (see run_matrix.py's own module
# docstring for the exact exit-code table: 0 full matrix matched, 1 any
# mismatch/error, 2 preflight/unknown-scenario, 3 ran clean but not the
# full matrix requested).
#
# Every precondition is checked and named before anything is built -
# "tự phát hiện thiếu infra và báo rõ, không giả định có sẵn" (M2.5 Step 3
# instruction this script was written against): no docker, no privileged-
# container support, no network to pull a base image, all fail loud here,
# never silently skip to "probably fine."
set -euo pipefail

KEEP=0
SCENARIOS=""
PROFILE=core
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP=1; shift ;;
    --scenarios) SCENARIOS="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
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
TREE="$(git -C "${REPO_ROOT}" rev-parse 'HEAD^{tree}')"
# The host is built from the working tree, the evidence names a commit/tree:
# they must be the same thing, or the evidence is about code nobody committed.
# (docs/evidence is excluded - it is this script's own output.)
DIRTY="$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=all -- . ':!docs/evidence' || true)"
if [ -n "$DIRTY" ] && [ "${ALLOW_DIRTY:-0}" != 1 ]; then
  die "the working tree differs from commit ${COMMIT} - commit first (or ALLOW_DIRTY=1 for a debugging run whose evidence must not be kept):
${DIRTY}"
fi

# packs/seaweedfs is still `maturity: draft` - dpagent installs a draft only
# with --allow-draft "and use a test VM" (docs/deploy.md). This disposable
# container is exactly that: the install here, its own acceptance suite
# (suites/seaweedfs) and the matrix on top ARE the proof a human reads before
# running `dpagent promote seaweedfs`. Promotion is not done by this script.
ALLOW_DRAFT=""
case "$PROFILE" in
  core)   SPEC=/opt/dpagent/examples/layer2-stack.yaml ;;
  bronze) SPEC=/opt/dpagent/examples/layer2-bronze-stack.yaml; ALLOW_DRAFT="--allow-draft" ;;
  *) die "unknown --profile ${PROFILE} (core | bronze)" ;;
esac

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
  export BRONZE_S3_ACCESS_KEY="$(openssl rand -hex 10)"
  export BRONZE_S3_SECRET_KEY="$(openssl rand -hex 20)"
  dpagent spec '"${SPEC}"' --yes '"${ALLOW_DRAFT}"'
' || die "bootstrap/install failed - see output above; nothing in this repo was touched, only the now-discarded container"

# ----------------------------------------------------------- re-verify

docker exec "${CONTAINER}" pg_isready >/dev/null 2>&1 || die "postgres installed but pg_isready failed post-install"
for svc in postgresql airflow-scheduler airflow-webserver; do
  state="$(docker exec "${CONTAINER}" systemctl is-active "$svc" 2>/dev/null || true)"
  [ "$state" = "active" ] || die "$svc is not active post-install (got: ${state:-<none>})"
done
docker exec "${CONTAINER}" bash -c 'sudo -n -u postgres true' \
  || die "passwordless sudo to postgres is not working inside the container - should be automatic for root via pam_rootok; something about this image changed that"
if [ "$PROFILE" = bronze ]; then
  state="$(docker exec "${CONTAINER}" systemctl is-active seaweedfs 2>/dev/null || true)"
  [ "$state" = "active" ] || die "seaweedfs is not active post-install (got: ${state:-<none>})"
  docker exec "${CONTAINER}" bash -c 'test -f /opt/seaweedfs/credentials.env' || die "the seaweedfs pack did not write its credentials file"
  docker exec "${CONTAINER}" /opt/dlt/.venv/bin/python -c 'import pyarrow, boto3, psycopg2' \
    || die "the dlt venv cannot import pyarrow/boto3/psycopg2 - packs/dlt did not install the bronze worker's dependencies"
fi
ok "postgres+dlt+dbt+airflow${PROFILE:+ (profile: ${PROFILE})} installed and verified running"

# -------------------------------------------------------------- run matrix

docker exec "${CONTAINER}" mkdir -p /opt/dpagent/tests/m25_acceptance
docker cp "${REPO_ROOT}/tests/m25_acceptance/registry.py" "${CONTAINER}:/opt/dpagent/tests/m25_acceptance/registry.py"
docker cp "${REPO_ROOT}/tests/m25_acceptance/run_matrix.py" "${CONTAINER}:/opt/dpagent/tests/m25_acceptance/run_matrix.py"

say "running the acceptance matrix driver"
set +e
docker exec -e "M25_ACCEPTANCE_COMMIT=${COMMIT}" "${CONTAINER}" \
  bash -c "cd /opt/dpagent && .venv/bin/python tests/m25_acceptance/run_matrix.py --profile ${PROFILE} ${SCENARIOS}"
DRIVER_RC=$?
set -e

# ----------------------------------------------------------- final leak audit

# Whatever the scenarios did - including the ones that fail on purpose - the
# host must end with nothing of theirs on it. Looked at directly, not trusted
# from the reports: databases, roles, published clones, shared-dbt models,
# registered DAGs, clone secrets, and (bronze profile) every object under the
# validation namespace.
LEAK_AUDIT="$(mktemp)"
docker exec "${CONTAINER}" bash -c '
leaks=0
section() { printf "== %s\n" "$1"; }
found()   { if [ -n "$1" ]; then printf "%s\n" "$1"; leaks=$((leaks+1)); else echo "(none)"; fi; }
section "databases named dpagent_fixture_*";  found "$(sudo -n -u postgres psql -At -c "select datname from pg_database where datname like '"'"'dpagent_fixture_%'"'"'")"
section "roles named dpagent_fixture_*";      found "$(sudo -n -u postgres psql -At -c "select rolname from pg_roles where rolname like '"'"'dpagent_fixture_%'"'"'")"
section "published validation clones";        found "$(ls /opt/dpagent/pipelines | grep -E "__validate__|hg_a2_promote" || true)"
section "shared dbt project clone models";    found "$(ls /opt/dbt/project/models 2>/dev/null | grep __validate__ || true)"
section "Airflow DAGs of clones";             found "$(su airflow -s /bin/bash -c "AIRFLOW_HOME=/opt/airflow/home /opt/airflow/.venv/bin/airflow dags list 2>/dev/null" | grep -E "__validate__|hg_a2_promote" || true)"
section "clone secrets in pipelines.env";     found "$(grep -E "DPAGENT_VALIDATE|HG_POC_|HG_A2_" /opt/airflow/home/pipelines.env 2>/dev/null | sed "s/=.*/=<redacted>/" || true)"
if [ -f /opt/seaweedfs/credentials.env ]; then
  . /opt/seaweedfs/credentials.env; export S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY
  section "S3 objects under dpagent-validate/"; found "$(python3 /opt/seaweedfs/bin/s3.py list hg-bronze dpagent-validate/ || true)"
  section "S3 objects under a2-promote/";       found "$(python3 /opt/seaweedfs/bin/s3.py list hg-bronze a2-promote/ || true)"
fi
echo; echo "leaks=${leaks}"
' > "${LEAK_AUDIT}" 2>&1 || true
cat "${LEAK_AUDIT}"
LEAKS="$(grep -oE '^leaks=[0-9]+' "${LEAK_AUDIT}" | cut -d= -f2)"

# --------------------------------------------------------- collect evidence

mkdir -p "${RESULTS_DIR}"
{
  echo "commit=${COMMIT}"
  echo "tree=${TREE}"
  echo "working_tree_clean=$([ -z "$DIRTY" ] && echo yes || echo NO)"
  echo "profile=${PROFILE}"
  echo "finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${RESULTS_DIR}/run-info.txt"
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
cp "${LEAK_AUDIT}" "${RESULTS_DIR}/leak-audit.txt"
ok "evidence written under ${RESULTS_DIR}"
if [ "${LEAKS:-x}" != "0" ]; then
  echo "XX the host still holds resources of finished scenarios (leaks=${LEAKS:-unknown}) - see leak-audit.txt" >&2
  [ "$DRIVER_RC" -ne 0 ] || DRIVER_RC=5
fi

exit "$DRIVER_RC"
