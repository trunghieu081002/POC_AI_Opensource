#!/usr/bin/env bash
# `dbt run` reporting success is not proof by itself - the postgres suite
# proves the storage path by reading back through an independent client, and
# this does the same: run the model, then query the actual table directly
# with psql rather than trusting dbt's own exit code.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
INSTALL_DIR="$(dp_param install_dir /opt/dbt)"
PROJECT_DIR="$(dp_param project_dir /opt/dbt/project)"
PROFILES_DIR="$(dirname "$PROJECT_DIR")/profiles"
DBT="${INSTALL_DIR}/.venv/bin/dbt"
HOST="$(dp_param host localhost)"
PORT="$(dp_param port 5432)"
DATABASE="$(dp_param database warehouse)"
SCHEMA="$(dp_param db_schema public)"
DB_USER="$(dp_param db_user dbt_user)"

OUTPUT="$("$DBT" run --project-dir "$PROJECT_DIR" --profiles-dir "$PROFILES_DIR" \
  --select "${NS}.good_model" 2>&1)" \
  || dp_fail "dbt run failed:
${OUTPUT}"
printf '%s\n' "$OUTPUT" | grep -qE "Completed successfully" \
  || dp_fail "dbt run did not report success:
${OUTPUT}"

PASSWORD="$(python3 -c "
import yaml
p = yaml.safe_load(open('${PROFILES_DIR}/profiles.yml'))
t = p['default']['outputs'][p['default']['target']]
print(t['password'])
")"

COUNT="$(PGPASSWORD="$PASSWORD" psql -h "$HOST" -p "$PORT" -U "$DB_USER" -d "$DATABASE" -tAX \
  -c "SELECT count(*) FROM ${SCHEMA}.good_model WHERE marker = '${NS}';")"
[ "$COUNT" = "2" ] || dp_fail "expected 2 rows in ${SCHEMA}.good_model, psql independently found ${COUNT} — dbt run did not really write what it claimed"

dp_ok "dbt run materialized ${SCHEMA}.good_model with the expected 2 rows, verified independently via psql"
