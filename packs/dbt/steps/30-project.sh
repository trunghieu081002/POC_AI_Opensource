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

# Group-shared, not world-readable: dbtread members (airflow, once it joins -
# see packs/airflow/steps/10-user.sh) can read the profile and traverse the
# project tree, everyone else cannot. setgid so target/ and logs/, which dbt
# itself creates the first time it runs, inherit the *group* too instead of
# landing owned by whichever user (root, during this pack's own acceptance
# suite, or airflow later) happened to run first.
#
# setgid alone is not enough for a *different* group member to then write
# there, though: it only fixes group ownership on new entries, not their
# permission bits, which come from the creating process's umask - root's own
# `dbt run` (this pack's suite) leaves logs/dbt.log and everything under
# target/ at the usual 644, group read-only. The next dbt invocation under a
# different user (airflow orchestrating it, exactly this stack's point) then
# fails with a raw `PermissionError: ... dbt.log` that looks nothing like a
# permissions problem from the traceback alone. A default ACL is what
# actually keeps this working regardless of which user creates a file next -
# `-m` fixes anything already here (this pack's own suite runs before this
# step's guard would ever skip it, but a project dropped in by hand might
# not be empty), `-d` covers everything dbt creates after that.
dp_run chgrp dbtread "$PROJECT_DIR" "$PROFILES_DIR"
dp_run chmod 2775 "$PROJECT_DIR"
dp_run chmod 2750 "$PROFILES_DIR"
dp_have setfacl || dp_pkg_install acl
if dp_have setfacl; then
  dp_run setfacl -R -m "g:dbtread:rwX" -d -m "g:dbtread:rwX" "$PROJECT_DIR"
else
  dp_warn "setfacl unavailable; dbtread members can read this project but may hit Permission denied writing logs/target if they did not create them first"
fi

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
# file readable only by root and the dbtread group, never echoed to a log.
dp_write "${PROFILES_DIR}/profiles.yml" 0640 "root:dbtread" <<EOF
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
