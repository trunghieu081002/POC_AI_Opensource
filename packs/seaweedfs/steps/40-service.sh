#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
DATA_DIR="$(dp_param data_dir /var/lib/seaweedfs)"
PORT="$(dp_param s3_port 8333)"
BIND="$(dp_param bind_address 127.0.0.1)"
VOLMB="$(dp_param volume_size_limit_mb 1024)"
VOLMAX="$(dp_param volume_max 100)"

# `weed server` = master + volume + filer + S3 gateway in one process: the
# smallest shape that is a real S3 endpoint. -ip.bind keeps every listener
# (master, volume, filer, s3) on one address.
dp_write /etc/systemd/system/seaweedfs.service 0644 <<UNIT
[Unit]
Description=SeaweedFS (weed server, S3 gateway)
After=network-online.target
Wants=network-online.target

[Service]
User=seaweedfs
Group=seaweedfs
ExecStart=${INSTALL_DIR}/bin/weed server -dir=${DATA_DIR} -ip.bind=${BIND} -ip=${BIND} -master.volumeSizeLimitMB=${VOLMB} -volume.max=${VOLMAX} -s3 -s3.port=${PORT} -s3.config=${INSTALL_DIR}/s3.json
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
UNIT

dp_run systemctl daemon-reload
# restart, not just enable --now: a re-run with a changed key must pick up the new s3.json
dp_run systemctl enable seaweedfs
dp_svc_restart seaweedfs
dp_wait_for_port "$PORT" 90
dp_ok "seaweedfs running (S3 on ${BIND}:${PORT})"
