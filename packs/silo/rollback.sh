#!/usr/bin/env bash
# Removes the package (which removes its own systemd unit) and data_dir -
# which destroys every bucket this instance ever held, the same trade-off
# airflow's own rollback makes for AIRFLOW_HOME. Does NOT remove the 'silo'
# system user the package's own sysusers.d entry created - low value,
# non-zero risk, same reasoning every other pack in this library applies to
# OS users, and removing it here is not this pack's job since it did not
# create it either.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/silo-lib.sh"

dp_require_root
DATA_DIR="$(sl_data_dir)"

if dp_svc_exists silo; then
  dp_svc_stop silo || dp_warn "could not stop silo"
fi

dp_pkg_remove silo

if [ -f "$(sl_env_file)" ]; then
  dp_run rm -f "$(sl_env_file)"
fi

dp_warn "removing ${DATA_DIR} - every bucket and object on this instance will be lost"
case "$DATA_DIR" in
  /var/lib/silo/*|/opt/*)
    if [ -d "$DATA_DIR" ]; then
      dp_run rm -rf "$DATA_DIR"
    fi ;;
  *) dp_warn "refusing to remove unexpected data_dir: ${DATA_DIR}" ;;
esac

dp_warn "left in place: the 'silo' system user (created by the package, not this pack)"
dp_ok "silo rolled back"
