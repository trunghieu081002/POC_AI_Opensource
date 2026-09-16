#!/usr/bin/env bash
# Removes the venv and the wrapper. Leaves pipeline manifests
# (pipelines/<name>/pipeline.yaml) and any dlt run state written under
# pipelines/<name>/build/ alone - those are dpagent's own artifacts, not
# this pack's, and rolling back the tool must not destroy a pipeline
# someone reviewed and committed.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/dlt)"

if [ -f /usr/local/bin/dlt ]; then
  dp_run rm -f /usr/local/bin/dlt
fi

case "$INSTALL_DIR" in
  /opt/*|/usr/local/*)
    if [ -d "${INSTALL_DIR}/.venv" ]; then
      dp_run rm -rf "${INSTALL_DIR}/.venv"
    fi
    if [ -f "${INSTALL_DIR}/selftest.env" ]; then
      dp_run rm -f "${INSTALL_DIR}/selftest.env"
    fi ;;
  *)
    dp_warn "refusing to remove unexpected install_dir: ${INSTALL_DIR}" ;;
esac

dp_ok "dlt rolled back"
