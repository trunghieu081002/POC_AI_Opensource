#!/usr/bin/env bash
# Two throwaway DAGs, manual-trigger only, dag_id namespaced with DP_TEST_NS:
#   ${NS}_ok   - a single task that succeeds
#   ${NS}_fail - a single task that deliberately raises
# `airflow dags reserialize` registers them in the metadata DB immediately,
# instead of waiting on the scheduler's own DAG-directory scan interval.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/airflow/af-lib.sh"

NS="${DP_TEST_NS:?}"
DAGS_DIR="$(af_home)/dags"
OK_FILE="${DAGS_DIR}/${NS}_ok.py"
FAIL_FILE="${DAGS_DIR}/${NS}_fail.py"

if [ -f "$OK_FILE" ] || [ -f "$FAIL_FILE" ]; then
  dp_warn "leftover self-test DAG(s) from a previous run; removing them"
  dp_run rm -f "$OK_FILE" "$FAIL_FILE"
fi

cat <<PYEOF | dp_write "$OK_FILE" 0644 airflow:airflow
import datetime
from airflow import DAG
from airflow.operators.python import PythonOperator

with DAG(
    dag_id="${NS}_ok",
    schedule_interval=None,
    start_date=datetime.datetime(2026, 1, 1),
    catchup=False,
    tags=["dpagent-selftest"],
) as dag:
    PythonOperator(task_id="succeed", python_callable=lambda: print("ok"))
PYEOF

cat <<PYEOF | dp_write "$FAIL_FILE" 0644 airflow:airflow
import datetime
from airflow import DAG
from airflow.operators.python import PythonOperator


def _boom():
    raise RuntimeError("dpagent selftest: this task is meant to fail")


with DAG(
    dag_id="${NS}_fail",
    schedule_interval=None,
    start_date=datetime.datetime(2026, 1, 1),
    catchup=False,
    tags=["dpagent-selftest"],
) as dag:
    PythonOperator(task_id="fail", python_callable=_boom, retries=0)
PYEOF

RESULT="$(af_run dags reserialize 2>&1)" || dp_fail "airflow dags reserialize failed:
${RESULT}"

af_run dags unpause "${NS}_ok" >/dev/null 2>&1
af_run dags unpause "${NS}_fail" >/dev/null 2>&1

dp_ok "deployed and registered ${NS}_ok and ${NS}_fail"
