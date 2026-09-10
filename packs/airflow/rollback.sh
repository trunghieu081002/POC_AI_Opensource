#!/usr/bin/env bash
# Removes the services, unit files and install_dir - which includes DAGs and
# logs under AIRFLOW_HOME, so this destroys real work if any was added. Does
# NOT touch the backend Postgres database (owned by the postgres pack) or
# remove the `airflow` system user (low value, non-zero risk; left in place
# like every other pack's OS-user decisions in this library).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

dp_require_root
INSTALL_DIR="$(af_install_dir)"

for unit in airflow-webserver airflow-scheduler; do
  if dp_svc_exists "$unit"; then
    dp_svc_stop "$unit" || dp_warn "could not stop ${unit}"
  fi
  if [ -f "/etc/systemd/system/${unit}.service" ]; then
    dp_run rm -f "/etc/systemd/system/${unit}.service"
  fi
done
dp_run systemctl daemon-reload

case "$INSTALL_DIR" in
  /opt/*)
    dp_warn "removing ${INSTALL_DIR} - this includes any DAGs and logs under it"
    if [ -d "$INSTALL_DIR" ]; then
      dp_run rm -rf "$INSTALL_DIR"
    fi ;;
  *)
    dp_warn "refusing to remove unexpected install_dir: ${INSTALL_DIR}" ;;
esac

dp_warn "left in place: the 'airflow' system user, and the backend Postgres database"
dp_ok "airflow rolled back"
