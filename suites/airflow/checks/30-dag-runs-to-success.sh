#!/usr/bin/env bash
# Trigger the throwaway "ok" DAG through the real scheduler/executor path (not
# `airflow tasks test`, which bypasses the scheduler entirely) and poll until
# it reaches a terminal state. This is the actual claim the pack makes: a
# submitted DAG gets run, not just that a process called "scheduler" exists.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/airflow/af-lib.sh"

NS="${DP_TEST_NS:?}"
DAG_ID="${NS}_ok"

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

[ "$STATE" = "success" ] || dp_fail "DAG ${DAG_ID} run ${RUN_ID} ended in state '${STATE}', expected 'success' (timed out waiting, or the task genuinely failed)"

dp_ok "DAG ${DAG_ID} (run ${RUN_ID}) ran through the real scheduler/executor and succeeded"
