#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/dlt)"
DLT_VERSION="$(dp_param dlt_version "1.*")"
PIP="${INSTALL_DIR}/.venv/bin/pip"

if [ "$DP_DRY_RUN" != "1" ]; then
  [ -x "$PIP" ] || dp_fail "venv missing at ${INSTALL_DIR}/.venv - the venv step should have created it"
fi

# postgres: the destination every pipeline in this stack lands into.
# sql_database: dlt's verified source for reading from a SQL database (what
# the odoo_postgres connector uses) - a separate extra from the destination
# even though both talk to postgres here, since the source side pulls in
# sqlalchemy rather than being psycopg2-only.
dp_run "$PIP" install --quiet "dlt[postgres,sql_database]==${DLT_VERSION}"

INSTALLED="$("${INSTALL_DIR}/.venv/bin/dlt" --version 2>/dev/null | head -1 || true)"
dp_ok "installed: ${INSTALLED:-dlt (version string unavailable)}"
