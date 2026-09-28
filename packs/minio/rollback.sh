#!/usr/bin/env bash
# Removes the service, unit file and install_dir/data_dir - which destroys
# every bucket this instance ever held, the same trade-off airflow's own
# rollback makes for AIRFLOW_HOME (DAGs/logs). Does NOT remove the `minio`
# system user, same reasoning as every other pack in this library.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/mn-lib.sh"

dp_require_root
INSTALL_DIR="$(mn_install_dir)"
DATA_DIR="$(mn_data_dir)"

if dp_svc_exists minio; then
  dp_svc_stop minio || dp_warn "could not stop minio"
fi
if [ -f /etc/systemd/system/minio.service ]; then
  dp_run rm -f /etc/systemd/system/minio.service
fi
dp_run systemctl daemon-reload

dp_warn "removing ${DATA_DIR} - every bucket and object on this instance will be lost"
case "$DATA_DIR" in
  /opt/*|/var/lib/*)
    if [ -d "$DATA_DIR" ]; then
      dp_run rm -rf "$DATA_DIR"
    fi ;;
  *) dp_warn "refusing to remove unexpected data_dir: ${DATA_DIR}" ;;
esac

case "$INSTALL_DIR" in
  /opt/*)
    if [ -d "$INSTALL_DIR" ]; then
      dp_run rm -rf "$INSTALL_DIR"
    fi ;;
  *) dp_warn "refusing to remove unexpected install_dir: ${INSTALL_DIR}" ;;
esac

dp_warn "left in place: the 'minio' system user"
dp_ok "minio rolled back"
