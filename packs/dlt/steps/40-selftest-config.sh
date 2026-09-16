#!/usr/bin/env bash
# Writes the resolved host/port/database/db_user/db_password to a
# root-only file the acceptance suite reads from, instead of the suite
# reading db_password back out of dpagent's own state.
#
# Why this step exists at all: a `secret: true` param is masked before
# dpagent's state DB ever stores it (packs/dbt's own profiles.yml exists for
# exactly this reason - see its steps/30-project.sh). A suite that resolves
# its params the normal way (state.get_install -> _stored_params, same path
# `dpagent test` uses) gets that mask back, not the real password - psql
# then blocks on an interactive password prompt instead of failing loudly.
# Found by actually running `dpagent install dlt` end to end, not by reading
# the masking code and guessing it would be fine.
#
# Not guarded on the file already existing: unlike dbt's project.yml (which
# can hold an operator's real hand-written models), this file holds nothing
# but generated self-test config, so a changed --set db_password=... must
# always overwrite it, not silently keep training on a stale password.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/dlt)"
HOST="$(dp_param host localhost)"
PORT="$(dp_param port 5432)"
DATABASE="$(dp_param database dlt_selftest)"
DB_USER="$(dp_param db_user dlt_user)"
DB_PASSWORD="$(dp_param db_password)"

dp_write "${INSTALL_DIR}/selftest.env" 0600 <<EOF
DLT_SELFTEST_HOST=${HOST}
DLT_SELFTEST_PORT=${PORT}
DLT_SELFTEST_DATABASE=${DATABASE}
DLT_SELFTEST_DB_USER=${DB_USER}
DLT_SELFTEST_DB_PASSWORD=${DB_PASSWORD}
EOF

dp_ok "wrote self-test connection config to ${INSTALL_DIR}/selftest.env"
