#!/usr/bin/env bash
# Negative check: the "fail" DAG's only task always raises. If Airflow reports
# this run as anything other than 'failed' — success, stuck running, or
# silently dropped — the pack's monitoring story is worthless: a real pipeline
# failure would go unnoticed the same way.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/airflow/af-lib.sh"

NS="${DP_TEST_NS:?}"
DAG_ID="${NS}_fail"

json_line() { printf '%s\n' "$1" | grep -E '^\s*[][{}]' | tail -1; }

TRIGGER_OUT="$(af_run dags trigger "$DAG_ID" -o json 2>&1)" \
  || dp_fail "airflow dags trigger ${DAG_ID} failed:
${TRIGGER_OUT}"
RUN_ID="$(json_line "$TRIGGER_OUT" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['dag_run_id'])")"
[ -n "$RUN_ID" ] || dp_fail "could not read a run_id back from trigger output:
${TRIGGER_OUT}"

STATE="queued"
for _ in $(seq 1 30); do
  LIST_OUT="$(af_run dags list-runs -d "$DAG_ID" -o json 2>&1)" || true
  STATE="$(json_line "$LIST_OUT" | python3 -c "
import sys, json
runs = json.load(sys.stdin)
match = [r for r in runs if r.get('run_id') == '${RUN_ID}']
print(match[0]['state'] if match else 'unknown')
" 2>/dev/null || echo unknown)"
  case "$STATE" in
    success|failed) break ;;
  esac
  sleep 3
done

[ "$STATE" = "failed" ] || dp_fail "DAG ${DAG_ID} run ${RUN_ID} ended in state '${STATE}', expected 'failed' — a task that always raises was not reported as a failure"

dp_ok "DAG ${DAG_ID} (run ${RUN_ID}), whose task always raises, was correctly reported as failed"
