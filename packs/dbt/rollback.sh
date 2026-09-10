#!/usr/bin/env bash
# Removes the venv and the wrapper. Deliberately leaves project_dir alone: it
# can hold real models an operator wrote by hand, and silently deleting someone
# else's dbt project is a far worse failure mode than leaving an unused folder.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/dbt)"
PROJECT_DIR="$(dp_param project_dir /opt/dbt/project)"

if [ -f /usr/local/bin/dbt ]; then
  dp_run rm -f /usr/local/bin/dbt
fi

case "$INSTALL_DIR" in
  /opt/*|/usr/local/*)
    if [ -d "${INSTALL_DIR}/.venv" ]; then
      dp_run rm -rf "${INSTALL_DIR}/.venv"
    fi ;;
  *)
    dp_warn "refusing to remove unexpected install_dir: ${INSTALL_DIR}" ;;
esac

dp_warn "left in place (may hold real work): ${PROJECT_DIR}"
dp_ok "dbt rolled back"
