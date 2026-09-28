#!/usr/bin/env bash
# Always runs to completion (no step-level guard beyond param-hash
# checkpointing in pack.yaml): restarting an already-correct service is
# cheap and this is how a changed param (e.g. a new port) actually takes
# effect on a re-run - same reasoning as airflow's own services step.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/mn-lib.sh"

dp_require_root
BIN="$(mn_bin)"
ENV_FILE="$(mn_env_file)"
DATA_DIR="$(mn_data_dir)"
PORT="$(dp_param port 9000)"
CONSOLE_PORT="$(dp_param console_port 9001)"

# $MINIO_OPTS/$MINIO_VOLUMES must stay literal in the unit file - systemd
# expands them from EnvironmentFile at service-start time, the same as
# MinIO's own official systemd unit. Escaped here so *this* shell (writing
# the file) does not expand them first into whatever they happen to be (or
# are not) in the install script's own environment.
dp_write /etc/systemd/system/minio.service 0644 <<EOF
[Unit]
Description=MinIO object storage (managed by dpagent)
After=network.target

[Service]
User=minio
Group=minio
EnvironmentFile=${ENV_FILE}
ExecStart=${BIN} server \$MINIO_OPTS \$MINIO_VOLUMES
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

dp_run systemctl daemon-reload
dp_svc_enable minio

if [ "$(dp_param open_firewall 0)" = "1" ]; then
  dp_firewall_allow "$PORT"
  dp_firewall_allow "$CONSOLE_PORT"
fi

dp_wait_for_port "$PORT" 60
dp_wait_for_port "$CONSOLE_PORT" 60

dp_ok "minio enabled - S3 API on :${PORT}, console on :${CONSOLE_PORT}, data in ${DATA_DIR}"
