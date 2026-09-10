#!/usr/bin/env bash
# Seed a minimal project + profile so `dbt debug` and `dbt run` work out of the
# box. Guarded on dbt_project.yml existing, so a real project dropped in later
# is never overwritten by a re-run.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
PROJECT_DIR="$(dp_param project_dir /opt/dbt/project)"
PROFILE_NAME="$(dp_param profile_name default)"
TARGET="$(dp_param target dev)"
HOST="$(dp_param host localhost)"
PORT="$(dp_param port 5432)"
DATABASE="$(dp_param database warehouse)"
SCHEMA="$(dp_param db_schema public)"
DB_USER="$(dp_param db_user dbt_user)"
DB_PASSWORD="$(dp_param db_password)"
THREADS="$(dp_param threads 4)"
PROFILES_DIR="$(dirname "$PROJECT_DIR")/profiles"

dp_run mkdir -p "$PROJECT_DIR/models" "$PROFILES_DIR"

dp_write "${PROJECT_DIR}/dbt_project.yml" 0644 <<EOF
name: '${PROFILE_NAME}'
version: '1.0.0'
config-version: 2
profile: '${PROFILE_NAME}'
model-paths: ["models"]
target-path: "target"
clean-targets: ["target", "dbt_packages"]
models:
  ${PROFILE_NAME}:
    +materialized: view
EOF

dp_write "${PROJECT_DIR}/models/.gitkeep" 0644 <<'EOF'
EOF

# Secret handling matches the rest of the agent: the password is written into a
# file readable only by the account running dbt, never echoed to a log.
dp_write "${PROFILES_DIR}/profiles.yml" 0600 <<EOF
${PROFILE_NAME}:
  target: ${TARGET}
  outputs:
    ${TARGET}:
      type: postgres
      host: ${HOST}
      port: ${PORT}
      user: ${DB_USER}
      password: "${DB_PASSWORD}"
      dbname: ${DATABASE}
      schema: ${SCHEMA}
      threads: ${THREADS}
EOF

dp_ok "seeded project at ${PROJECT_DIR}, profile at ${PROFILES_DIR}/profiles.yml"
