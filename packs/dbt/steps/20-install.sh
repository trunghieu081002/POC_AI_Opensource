#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/dbt)"
ADAPTER="$(dp_param adapter postgres)"
DBT_VERSION="$(dp_param dbt_version "1.8.*")"
PIP="${INSTALL_DIR}/.venv/bin/pip"

[ -x "$PIP" ] || dp_fail "venv missing at ${INSTALL_DIR}/.venv - the venv step should have created it"

case "$ADAPTER" in
  postgres) ADAPTER_PKG="dbt-postgres" ;;
  *) dp_fail "adapter '${ADAPTER}' is not wired up yet - only postgres ships in this pack" ;;
esac

# dbt-postgres pulls in dbt-core and psycopg2-binary transitively; pinning both
# to the same version line keeps core/adapter compatible.
dp_run "$PIP" install --quiet "dbt-core==${DBT_VERSION}" "${ADAPTER_PKG}==${DBT_VERSION}"

INSTALLED="$("${INSTALL_DIR}/.venv/bin/dbt" --version 2>/dev/null | head -1 || true)"
dp_ok "installed: ${INSTALLED:-dbt (version string unavailable)}"
