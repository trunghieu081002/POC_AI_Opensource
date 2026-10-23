#!/usr/bin/env bash
# Runs INSIDE the disposable host (scripts/quickstart-acceptance-ci.sh --demo reference|ai). Plays the operator of
# docs/demo.md: examples/demo/demo.sh step by step (product commands only), with an independent check after each
# milestone and the refusals the demo must show. DEMO_DRAFT (from the runner): ai | reference.
#   inside.sh first    layer 1 install, layer 3 draft + approval, layer 2 deploy/run, schedule, dashboard + viewer API
#   inside.sh second   (after the host's viewer browser check) bad data blocked, recovery, status, undeploy + audit
#   inside.sh show     a host for a live demo: layer 1 + the draft, then it STOPS before approval (no cleanup) - the
#                      presenter's reviewer approves live (docs/demo-show.md)
set -euo pipefail
PHASE="${1:?first|second|show}"
DRAFT="${DEMO_DRAFT:?ai|reference|artifact}"
cd /opt/dpagent
OUT=/root/quickstart-evidence          # the runner copies this directory out
mkdir -p "$OUT"
DEMO=examples/demo/demo.sh
PY=/opt/dpagent/.venv/bin/python
SPY=/opt/superset/.venv/bin/python
EX=tests/llm_verification/cases/fulfilment_sla/expected.yaml

ENV=/root/demo-dev.env
if [ ! -f "$ENV" ]; then
  umask 077
  cp examples/demo/dev.env.example "$ENV"
  for key in FS_SRC_PASSWORD WAREHOUSE_DB_PASSWORD AIRFLOW_DB_PASSWORD AIRFLOW_ADMIN_PASSWORD DLT_DB_PASSWORD \
             SUPERSET_METADATA_PASSWORD SUPERSET_ADMIN_PASSWORD SUPERSET_WAREHOUSE_PASSWORD FS_VIEWER_PASSWORD; do
    sed -i "s/^${key}=.*/${key}=$(openssl rand -hex 16)/" "$ENV"
  done
  sed -i "s/^SUPERSET_SECRET_KEY=.*/SUPERSET_SECRET_KEY=$(openssl rand -hex 32)/" "$ENV"
  umask 022
fi
grep -q change-me "$ENV" && { echo "unfilled value in the environment file" >&2; exit 2; }
set -a; . "$ENV"; set +a
scan_secrets() {   # no secret value from the environment file or the model credential may reach the evidence
  local rc=$? value key found=""
  local -a values=()
  while IFS='=' read -r key value; do
    case "$key" in *PASSWORD*|*SECRET*) values+=("$value") ;; esac
  done < "$ENV"
  for key in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY; do
    if [ -n "${!key:-}" ]; then values+=("${!key}"); fi
  done
  for value in "${values[@]}"; do
    [ ${#value} -ge 8 ] || continue
    if grep -rqF -- "$value" "$OUT"; then found=1; fi
  done
  if [ -n "$found" ]; then echo "a secret value found in evidence - evidence removed" >&2; rm -rf "$OUT"; exit 99; fi
  echo "secret scan ($PHASE): no environment-file secret or model credential in evidence" >> "$OUT/steps.log"
  exit "$rc"
}
trap scan_secrets EXIT

step() {   # step <label> <command...>: run, keep its log and exit code, stop on failure
  local label="$1"; shift
  echo "== $label: $*" | tee -a "$OUT/steps.log"
  set +e; "$@" > "$OUT/$label.log" 2>&1; local rc=$?; set -e
  echo "$label rc=$rc" | tee -a "$OUT/steps.log"
  [ "$rc" -eq 0 ] || { tail -40 "$OUT/$label.log"; exit "$rc"; }
}
refused() {   # refused <label> <exit code> <reason text> <command...>: must fail with exactly that code and reason
  local label="$1" want="$2" reason="$3"; shift 3
  set +e; COLUMNS=400 "$@" > "$OUT/$label.log" 2>&1; local rc=$?; set -e
  echo "$label rc=$rc (want $want)" | tee -a "$OUT/steps.log"
  [ "$rc" -eq "$want" ] || { tail -20 "$OUT/$label.log"; echo "$label: exit $rc, want $want" >&2; exit 1; }
  tr -s ' \n' ' ' < "$OUT/$label.log" | grep -qF -- "$reason" \
    || { tail -20 "$OUT/$label.log"; echo "$label: refused for another reason (want: $reason)" >&2; exit 1; }
}
ok() { echo "check $1 ok" | tee -a "$OUT/steps.log"; }
meta() { runuser -u postgres -- psql -X -At -v ON_ERROR_STOP=1 -d superset_meta -c "$1"; }
fingerprint() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' \
                  /var/lib/dpagent-bi-assets/fulfil-sla/validation.json; }
dags() { "$PY" -c 'import subprocess, sys; from dpagent.pipelines import deploy
sys.exit(subprocess.run(deploy._airflow_cli_command("dags", "list", "-o", "plain")).returncode)'; }

if [ "$PHASE" = first ] || [ "$PHASE" = show ]; then
  # ---- layer 1: the stack
  step 01-preflight bash "$DEMO" preflight
  step 02-install   bash "$DEMO" install
  { for v in /opt/dpagent/.venv /opt/airflow/.venv /opt/superset/.venv /opt/dbt/.venv /opt/dlt/.venv; do
      [ -x "$v/bin/python" ] && { echo "# $v"; "$v/bin/python" -c 'import importlib.metadata as m
print("\n".join(sorted({"%s==%s" % (d.metadata["Name"], d.version) for d in m.distributions()}, key=str.lower)))'; }
    done; psql --version 2>/dev/null; } > "$OUT/dependency-versions.txt" || true
  step 03-source    bash "$DEMO" source

  # ---- layer 3: the draft (ai: the model through `pipeline synth`; reference: labelled hand-written)
  export DEMO_TRACE_DIR="$OUT/ai-trace"
  set +e; bash "$DEMO" synth > "$OUT/04-synth.log" 2>&1; rc=$?; set -e
  echo "04-synth rc=$rc draft=$DRAFT" | tee -a "$OUT/steps.log"
  cp pipelines/fulfilment_sla/.draft-origin "$OUT/draft-origin.txt" 2>/dev/null || echo "no draft" > "$OUT/draft-origin.txt"
  [ -d pipelines/fulfilment_sla ] && cp -r pipelines/fulfilment_sla "$OUT/draft" || true
  if [ "$DRAFT" = ai ]; then   # never a silent fallback: an ai run is the model's draft or it fails
    "$PY" tests/demo_acceptance/ai_summary.py "$OUT/ai-trace/trace.json" "$rc" > "$OUT/ai-summary.json" \
      || echo "{\"error\": \"no readable trace\", \"synth_exit_code\": $rc}" > "$OUT/ai-summary.json"
    tee -a "$OUT/steps.log" < "$OUT/ai-summary.json"
    grep -q "^model " "$OUT/draft-origin.txt" || { tail -20 "$OUT/04-synth.log"; echo "an ai run without a model draft" >&2; exit 1; }
  fi
  if [ "$DRAFT" = artifact ]; then   # the accepted model draft, imported: no synth ran here, no credential is present
    grep -q "^model .* imported from .* matched the trace's sha256; no model call on this host" "$OUT/draft-origin.txt" \
      || { tail -20 "$OUT/04-synth.log"; echo "not an imported model draft" >&2; exit 1; }
    [ ! -e "$OUT/ai-trace" ] || { echo "a synth trace was written on an artifact host" >&2; exit 1; }
    for key in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY DPAGENT_MODEL; do
      [ -z "${!key:-}" ] || { echo "$key is set on an artifact host" >&2; exit 1; }
    done
    ok "artifact-imported (files = the accepted trace's hashes; no model, no credential on this host)"
  fi
  [ "$rc" -eq 0 ] || { tail -60 "$OUT/04-synth.log"; exit "$rc"; }
  [ ! -e pipelines/fulfilment_sla/.approved.yaml ] || { echo "the draft step approved something" >&2; exit 1; }
  ok "draft-not-approved (synth/validate never approve)"
  if [ "$PHASE" = show ]; then
    echo "SHOW READY: draft validated, nothing approved - a person approves live (docs/demo-show.md)" | tee -a "$OUT/steps.log"
    exit 0
  fi

  # nothing deploys before a person approves; approve itself refuses without a name
  refused 05a-deploy-before-approval 1 "fulfilment_sla is maturity: draft (never reviewed)" \
    dpagent pipeline deploy fulfilment_sla --yes
  refused 05b-approve-without-a-person 2 "set DEMO_APPROVED_BY" env DEMO_APPROVED_BY= bash "$DEMO" approve
  DEMO_APPROVED_BY=demo.reviewer step 05-approve bash "$DEMO" approve

  # ---- layer 2: deploy, run, compare
  step 06-deploy bash "$DEMO" deploy
  "$PY" examples/demo/check_fulfil.py "$EX" > "$OUT/check-after-first-run.json" \
    || { cat "$OUT/check-after-first-run.json"; exit 1; }
  ok "after-first-run (warehouse = hand-calculated expected; landing = source rows once)"
  step 07-schedule bash "$DEMO" schedule
  "$PY" tests/demo_acceptance/check_run.py schedule > "$OUT/check-schedule.json" \
    || { cat "$OUT/check-schedule.json"; echo "Airflow does not hold the daily 02:00 UTC schedule (BRD)" >&2; exit 1; }
  ok "schedule-deployed (Airflow: 0 2 * * *, unpaused, next run known; scheduler-triggered runs so far: $("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["scheduler_triggered_runs"])' "$OUT/check-schedule.json"))"

  # ---- BI: the hand-written dashboard artifact, reviewed fingerprint, viewer
  step 08-bi-validate bash "$DEMO" bi-validate
  fingerprint > "$OUT/fingerprint-v1.txt"
  refused 09a-bi-deploy-unreviewed 3 "Validate and review the new fingerprint before supplying approved_sha256." \
    env DEMO_BI_APPROVED_BY=demo.reviewer \
    DEMO_BI_APPROVED_SHA256="$(printf '0%.0s' $(seq 64))" bash "$DEMO" bi-deploy
  "$SPY" tests/quickstart_acceptance/check.py bi-absent fulfil-sla > "$OUT/check-after-refused-bi.json" \
    || { cat "$OUT/check-after-refused-bi.json"; exit 1; }
  ok "after-refused-bi (nothing deployed)"
  DEMO_BI_APPROVED_BY=demo.reviewer DEMO_BI_APPROVED_SHA256="$(cat "$OUT/fingerprint-v1.txt")" \
    step 09-bi-deploy bash "$DEMO" bi-deploy
  "$SPY" tests/quickstart_acceptance/check_viewer.py fulfil.viewer FS_VIEWER_PASSWORD fulfil-sla - \
    > "$OUT/check-viewer-api.json" || { cat "$OUT/check-viewer-api.json"; exit 1; }
  ok "viewer-api (as fulfil.viewer: chart = expected, filter = expected, every write refused)"
  echo "PHASE first PASS" | tee -a "$OUT/steps.log"
  exit 0
fi

# ---------------------------------------------------------------- second phase: operations
"$PY" tests/demo_acceptance/check_run.py snapshot > "$OUT/report-before-bad-data.json"
step 10-bad-data bash "$DEMO" bad-data
RUN=$(sed -n 's/.*dpagent run \([0-9][0-9]*\).*/\1/p' "$OUT/10-bad-data.log" | tail -1)
[ -n "$RUN" ] || { echo "no dpagent run id in the bad-data run's output" >&2; exit 1; }
dpagent pipeline audit "$RUN" > "$OUT/10-audit-run-$RUN.log" 2>&1 || true
"$PY" tests/demo_acceptance/check_run.py blocked "$RUN" "$OUT/report-before-bad-data.json" > "$OUT/check-bad-data-run-$RUN.json" \
  || { cat "$OUT/check-bad-data-run-$RUN.json"; exit 1; }
ok "bad-data-blocked (run $RUN: failed; landing failed on exactly the stock_picking unique gate; no later stage ran; report table same relation, same rows)"

step 11-fix-data bash "$DEMO" fix-data
"$PY" examples/demo/check_fulfil.py "$EX" > "$OUT/check-after-recovery.json" || { cat "$OUT/check-after-recovery.json"; exit 1; }
ok "recovery (source fixed, rerun = expected, landing without duplicates)"

step 12-status bash "$DEMO" status
grep -q fulfilment_sla "$OUT/12-status.log"
"$PY" tests/demo_acceptance/check_run.py schedule > "$OUT/check-schedule-before-undeploy.json" || true
ok "runs-by-type before undeploy: $("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["runs_by_type"])' "$OUT/check-schedule-before-undeploy.json")"

step 13-undeploy bash "$DEMO" undeploy
"$SPY" tests/quickstart_acceptance/check.py bi-absent fulfil-sla > "$OUT/check-after-undeploy-bi.json" \
  || { cat "$OUT/check-after-undeploy-bi.json"; exit 1; }
n=$(meta "SELECT (SELECT count(*) FROM ab_user WHERE username = 'fulfil.viewer') + (SELECT count(*) FROM ab_role WHERE name = 'dpagent-viewer-fulfil-sla')")
[ "$n" = 0 ] || { echo "viewer account/role remain after undeploy ($n)" >&2; exit 1; }
list=$(dags 2>"$OUT/dags-list.err") || { cat "$OUT/dags-list.err"; echo "could not list Airflow's DAGs" >&2; exit 1; }
if printf '%s\n' "$list" | grep -qw fulfilment_sla; then echo "DAG still present after undeploy" >&2; exit 1; fi
if grep -q '^FS_SRC_' /opt/airflow/home/pipelines.env 2>/dev/null; then echo "pipeline secrets not released" >&2; exit 1; fi
MODELS="$("$PY" -c 'from dpagent.pipelines import deploy; print(deploy._dbt_project_dir())')/models/fulfilment_sla"
[ ! -e "$MODELS" ] || { echo "published dbt models still present: $MODELS" >&2; exit 1; }
ok "undeploy-audit (dashboard UUIDs, viewer, DAG via Airflow, secrets, dbt models gone; warehouse data kept by design)"
echo PASS | tee -a "$OUT/steps.log"
