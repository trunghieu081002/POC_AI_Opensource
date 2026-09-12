#!/usr/bin/env bash
# Two throwaway models, isolated under models/dpagent_selftest/ so they never
# collide with whatever real project the operator has added:
#   good_model  - two known rows, used to prove the write path
#   bad_model   - a deliberate uniqueness violation, used to prove dbt test
#                 actually catches bad data rather than rubber-stamping it
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
PROJECT_DIR="$(dp_param project_dir /opt/dbt/project)"
MODELS_DIR="${PROJECT_DIR}/models/${NS}"

if [ -d "$MODELS_DIR" ]; then
  dp_warn "leftover ${MODELS_DIR} from a previous run; removing it"
  dp_run rm -rf "$MODELS_DIR"
fi

dp_run mkdir -p "$MODELS_DIR"

{
  echo "{{ config(materialized='table') }}"
  echo "select 1 as id, '${NS}' as marker"
  echo "union all"
  echo "select 2 as id, '${NS}' as marker"
} | dp_write "${MODELS_DIR}/good_model.sql" 0664

{
  echo "{{ config(materialized='table') }}"
  echo "select 1 as id"
  echo "union all"
  echo "select 1 as id  -- deliberate duplicate: breaks the unique test below"
} | dp_write "${MODELS_DIR}/bad_model.sql" 0664

cat <<EOF | dp_write "${MODELS_DIR}/schema.yml" 0664
version: 2
models:
  - name: good_model
    columns:
      - name: id
        tests: [not_null, unique]
  - name: bad_model
    columns:
      - name: id
        tests: [unique]
EOF

dp_ok "throwaway models written under ${MODELS_DIR}"
