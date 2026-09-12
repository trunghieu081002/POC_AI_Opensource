#!/usr/bin/env bash
# Runs whatever happened above, including after a crash mid-suite. Every
# removal is guarded so a partial setup tears down cleanly.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
PROJECT_DIR="$(dp_param project_dir /opt/dbt/project)"
PROFILES_DIR="$(dirname "$PROJECT_DIR")/profiles"
HOST="$(dp_param host localhost)"
PORT="$(dp_param port 5432)"
DATABASE="$(dp_param database warehouse)"
SCHEMA="$(dp_param db_schema public)"
DB_USER="$(dp_param db_user dbt_user)"

if [ -f "${PROFILES_DIR}/profiles.yml" ]; then
  PASSWORD="$(python3 -c "
import yaml
p = yaml.safe_load(open('${PROFILES_DIR}/profiles.yml'))
t = p['default']['outputs'][p['default']['target']]
print(t['password'])
" 2>/dev/null || true)"

  if [ -n "${PASSWORD:-}" ]; then
    PGPASSWORD="$PASSWORD" psql -h "$HOST" -p "$PORT" -U "$DB_USER" -d "$DATABASE" -v ON_ERROR_STOP=0 -q \
      -c "DROP TABLE IF EXISTS ${SCHEMA}.good_model;" \
      -c "DROP TABLE IF EXISTS ${SCHEMA}.bad_model;" >/dev/null 2>&1 || true
  fi
fi

dp_run rm -rf "${PROJECT_DIR}/models/${NS}"

dp_ok "throwaway models and tables removed"
