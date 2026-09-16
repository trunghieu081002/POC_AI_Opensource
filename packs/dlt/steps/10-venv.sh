#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/dlt)"

PYTHON="$(dp_find_python 3 9 3 12 || true)"
if [ "$DP_DRY_RUN" != "1" ]; then
  [ -n "$PYTHON" ] || dp_fail "no python 3.9-3.12 on PATH (python-modern should have provided one)"
fi

dp_run mkdir -p "$INSTALL_DIR"
dp_run "$PYTHON" -m venv "${INSTALL_DIR}/.venv"
dp_run "${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip wheel

dp_ok "venv created at ${INSTALL_DIR}/.venv using ${PYTHON}"
