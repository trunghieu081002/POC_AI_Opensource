#!/usr/bin/env bash
# Runs INSIDE the disposable host, from the clean source copy at /opt/dpagent (scripts/quickstart-acceptance-ci.sh).
# Plays the operator of docs/quickstart.md: fills an environment file, runs examples/quickstart/walkthrough.sh
# step by step (product commands only), and reads values a person would read (the BI fingerprint). After each
# milestone an independent check (tests/quickstart_acceptance/check.py) compares with hand-calculated values.
set -euo pipefail
cd /opt/dpagent
OUT=/root/quickstart-evidence
mkdir -p "$OUT"
W=examples/quickstart/walkthrough.sh
PY=/opt/dpagent/.venv/bin/python

# --- the operator's environment file (random secrets; never written to evidence)
umask 077
ENV=/root/quickstart-dev.env
sed -e "s/^QS_SRC_PASSWORD=.*/QS_SRC_PASSWORD=$(openssl rand -hex 16)/" \
    -e "s/^QS_WH_PASSWORD=.*/QS_WH_PASSWORD=$(openssl rand -hex 16)/" \
    -e "s/^AIRFLOW_DB_PASSWORD=.*/AIRFLOW_DB_PASSWORD=$(openssl rand -hex 16)/" \
    -e "s/^AIRFLOW_ADMIN_PASSWORD=.*/AIRFLOW_ADMIN_PASSWORD=$(openssl rand -hex 16)/" \
    -e "s/^DLT_DB_PASSWORD=.*/DLT_DB_PASSWORD=$(openssl rand -hex 16)/" \
    -e "s/^SUPERSET_METADATA_PASSWORD=.*/SUPERSET_METADATA_PASSWORD=$(openssl rand -hex 16)/" \
    -e "s/^SUPERSET_SECRET_KEY=.*/SUPERSET_SECRET_KEY=$(openssl rand -hex 32)/" \
    -e "s/^SUPERSET_ADMIN_PASSWORD=.*/SUPERSET_ADMIN_PASSWORD=$(openssl rand -hex 16)/" \
    -e "s/^SUPERSET_WAREHOUSE_PASSWORD=.*/SUPERSET_WAREHOUSE_PASSWORD=$(openssl rand -hex 16)/" \
    -e "s/^QS_APPROVED_BY=.*/QS_APPROVED_BY=quickstart.reviewer/" \
    examples/quickstart/dev.env.example > "$ENV"
grep -q change-me "$ENV" && { echo "unfilled value in the environment file" >&2; exit 2; }
set -a; . "$ENV"; set +a
umask 022
scan_secrets() {   # never keep evidence that contains a secret value from the environment file (any exit)
  local rc=$?
  while IFS='=' read -r key value; do
    case "$key" in *PASSWORD*|*SECRET*) ;; *) continue ;; esac
    [ ${#value} -ge 8 ] || continue
    if grep -rqF -- "$value" "$OUT"; then
      echo "secret $key found in evidence - evidence removed" >&2; rm -rf "$OUT"; exit 99
    fi
  done < "$ENV"
  echo "secret scan: no environment-file secret value in evidence" >> "$OUT/steps.log"
  exit "$rc"
}
trap scan_secrets EXIT

step() {   # step <label> <command...>: run, keep its full log and exit code, stop on failure
  local label="$1"; shift
  echo "== $label: $*" | tee -a "$OUT/steps.log"
  set +e; "$@" > "$OUT/$label.log" 2>&1; local rc=$?; set -e
  echo "$label rc=$rc" | tee -a "$OUT/steps.log"
  [ "$rc" -eq 0 ] || { tail -40 "$OUT/$label.log"; exit "$rc"; }
}
check() {  # check <label> <args...>   (Superset's venv: it has requests and PyYAML)
  local label="$1"; shift
  /opt/superset/.venv/bin/python tests/quickstart_acceptance/check.py "$@" > "$OUT/check-$label.json" 2>&1 \
    || { echo "CHECK FAILED: $label"; cat "$OUT/check-$label.json"; exit 1; }
  echo "check $label ok" | tee -a "$OUT/steps.log"
}
fingerprint() { python3 -c 'import json; print(json.load(open("/var/lib/dpagent-bi-assets/quickstart-sales/validation.json"))["fingerprint"])'; }

step 01-install     bash "$W" install
step 02-source      bash "$W" source
step 03-validate    bash "$W" validate
step 04-approve     bash "$W" approve
step 05-deploy      bash "$W" deploy
"$PY" tests/quickstart_acceptance/check.py warehouse > "$OUT/check-after-first-run.json" || { cat "$OUT/check-after-first-run.json"; exit 1; }
echo "check after-first-run ok" | tee -a "$OUT/steps.log"

step 06-bi-validate bash "$W" bi-validate
FP1="$(fingerprint)"; echo "$FP1" > "$OUT/fingerprint-v1.txt"

# negative: a fingerprint nobody reviewed must not deploy anything
set +e
QS_BI_APPROVED_BY=quickstart.reviewer QS_BI_APPROVED_SHA256=0000000000000000000000000000000000000000000000000000000000000000 \
  bash "$W" bi-deploy > "$OUT/07-bi-deploy-wrong-fingerprint.log" 2>&1
rc=$?; set -e
echo "07-bi-deploy-wrong-fingerprint rc=$rc (must be non-zero)" | tee -a "$OUT/steps.log"
[ "$rc" -ne 0 ] || { echo "a wrong fingerprint deployed" >&2; exit 1; }
check after-refused-deploy bi-absent

QS_BI_APPROVED_BY=quickstart.reviewer QS_BI_APPROVED_SHA256="$FP1" step 08-bi-deploy bash "$W" bi-deploy
check after-bi-deploy bi "Quickstart sales: completed orders and revenue by month"
cp "$OUT/check-after-bi-deploy.json" "$OUT/ids-v1.json"

# update: a reviewed content change -> new revision -> validate -> new fingerprint -> deploy, same identities
sed -i 's/^dashboard_title: .*/dashboard_title: '"'"'Quickstart sales (v2): completed orders and revenue by month'"'"'/' \
  projects/quickstart-sales/bundle/dashboards/overview.yaml
export QS_BI_REVISION=v2
step 09-bi-validate-v2 bash "$W" bi-validate
FP2="$(fingerprint)"; echo "$FP2" > "$OUT/fingerprint-v2.txt"
[ "$FP2" != "$FP1" ] || { echo "the content change did not change the fingerprint" >&2; exit 1; }
QS_BI_APPROVED_BY=quickstart.reviewer QS_BI_APPROVED_SHA256="$FP2" step 10-bi-deploy-v2 bash "$W" bi-deploy
check after-update bi "Quickstart sales (v2): completed orders and revenue by month"
python3 - "$OUT/ids-v1.json" "$OUT/check-after-update.json" <<'PY' | tee -a "$OUT/steps.log"
import json, sys
a, b = (json.load(open(p))["ids"] for p in sys.argv[1:])
assert a == b, f"identities changed on update: {a} -> {b}"
print("check identities-stable-on-update ok", a)
PY

step 11-rerun       bash "$W" rerun
"$PY" tests/quickstart_acceptance/check.py warehouse > "$OUT/check-after-rerun.json" || { cat "$OUT/check-after-rerun.json"; exit 1; }
echo "check after-rerun (no duplicates) ok" | tee -a "$OUT/steps.log"
check after-rerun-bi bi "Quickstart sales (v2): completed orders and revenue by month"

step 12-status      bash "$W" status
grep -q quickstart_sales "$OUT/12-status.log"

step 13-undeploy    bash "$W" undeploy
check after-undeploy bi-absent
# scoped: the pipeline is gone from Airflow and the published tree; the Superset runtime still answers
if su airflow -s /bin/bash -c "AIRFLOW_HOME=/opt/airflow/home /opt/airflow/.venv/bin/airflow dags list 2>/dev/null" | grep -q quickstart_sales; then
  echo "DAG still present after undeploy" >&2; exit 1; fi
# (the source tree is NOT removed when dpagent runs from /opt/dpagent: published == source there, by design)
MODELS="$("$PY" -c 'from dpagent.pipelines import deploy; print(deploy._dbt_project_dir())')/models/quickstart_sales"
[ ! -e "$MODELS" ] || { echo "published dbt models still present: $MODELS" >&2; exit 1; }
if grep -q '^QS_' /opt/airflow/home/pipelines.env 2>/dev/null; then echo "pipeline secrets not released" >&2; exit 1; fi
[ -d pipelines/quickstart_sales ] || { echo "undeploy removed the operator's source tree" >&2; exit 1; }
curl -fsS --noproxy '*' -o /dev/null http://127.0.0.1:8088/health
echo "check scoped-undeploy ok (DAG, dbt models, secrets and BI assets gone; source kept; Superset runtime up)" | tee -a "$OUT/steps.log"
echo PASS | tee -a "$OUT/steps.log"
