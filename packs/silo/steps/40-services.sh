#!/usr/bin/env bash
# The package installs its own /usr/lib/systemd/system/silo.service (see
# silo-lib.sh's comment) - unlike every other pack in this library, there is
# no unit file for this step to write. Always runs to completion (no
# step-level guard beyond param-hash checkpointing in pack.yaml): restarting
# an already-correct service is cheap and this is how a changed param (e.g.
# a new port) actually takes effect on a re-run.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/silo-lib.sh"

dp_require_root
PORT="$(dp_param port 9000)"
CONSOLE_PORT="$(dp_param console_port 9001)"

dp_run systemctl daemon-reload
dp_svc_enable silo

if [ "$(dp_param open_firewall 0)" = "1" ]; then
  dp_firewall_allow "$PORT"
  dp_firewall_allow "$CONSOLE_PORT"
fi

dp_wait_for_port "$PORT" 60
dp_wait_for_port "$CONSOLE_PORT" 60

dp_ok "silo enabled - S3 API on :${PORT}, console on :${CONSOLE_PORT}, data in $(sl_data_dir)"
