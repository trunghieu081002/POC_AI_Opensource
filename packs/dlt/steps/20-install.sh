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
# the odoo_postgres and sql_server connectors both use) - a separate extra
# from the destination even though both talk to postgres here, since the
# source side pulls in sqlalchemy rather than being psycopg2-only.
dp_run "$PIP" install --quiet "dlt[postgres,sql_database]==${DLT_VERSION}"

# pymssql: the sql_server connector's own SQLAlchemy driver
# (runtime.py's _connection_url uses "mssql+pymssql") - a prebuilt wheel,
# not pyodbc, specifically to avoid this step also needing the Microsoft
# ODBC Driver system package (a whole separate apt/dnf repo) on every host
# this pack installs on. Pinned loosely, same convention as DLT_VERSION.
dp_run "$PIP" install --quiet "pymssql>=2.3,<3"

# elasticsearch: the elasticsearch connector's own client - no built-in dlt
# source for it (unlike sql_database/rest_api), so extract.py hand-rolls a
# dlt.resource per index using this client's own scan() scroll-API helper
# directly, the same shape the csv connector already uses.
dp_run "$PIP" install --quiet "elasticsearch>=8,<10"

# google-api-python-client/google-auth: the google_sheets connector's own
# client - no built-in dlt source for it either, hand-rolled the same way
# as elasticsearch: one dlt.resource per sheet, the Sheets API v4's own
# values.get() call reading it. Service-account auth only (never an
# interactive OAuth flow, which cannot run unattended inside an Airflow
# task).
dp_run "$PIP" install --quiet "google-api-python-client>=2,<3" "google-auth>=2,<3"

INSTALLED="$("${INSTALL_DIR}/.venv/bin/dlt" --version 2>/dev/null | head -1 || true)"
dp_ok "installed: ${INSTALLED:-dlt (version string unavailable)}"
