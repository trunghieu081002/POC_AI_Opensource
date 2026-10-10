#!/usr/bin/env bash
# Removes the service, unit, binary and config. Leaves the data directory:
# objects are someone's data, and rolling back the tool must not silently
# destroy them (the path is printed so it can be removed deliberately).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
DATA_DIR="$(dp_param data_dir /var/lib/seaweedfs)"

if dp_svc_exists seaweedfs 2>/dev/null; then
  dp_run systemctl disable --now seaweedfs || true
fi
dp_run rm -f /etc/systemd/system/seaweedfs.service /etc/seaweedfs/master.toml
dp_run systemctl daemon-reload
case "$INSTALL_DIR" in
  /opt/*|/usr/local/*) dp_run rm -rf "$INSTALL_DIR" ;;
  *) dp_warn "refusing to remove unexpected install_dir: ${INSTALL_DIR}" ;;
esac
dp_warn "data kept at ${DATA_DIR} - remove it by hand if the objects are not needed"
dp_ok "seaweedfs rolled back"
