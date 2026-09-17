#!/usr/bin/env bash
# Two systemd units, both fed by the same EnvironmentFile the config step wrote.
# Always runs to completion (no step-level guard beyond param-hash checkpointing
# in pack.yaml): restarting an already-correct service is cheap and this is how
# a changed param (e.g. a new port) actually takes effect on a re-run.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

dp_require_root
ENV_FILE="$(af_env_file)"
PIPELINE_SECRETS_FILE="$(af_pipeline_secrets_file)"
AIRFLOW_BIN="$(af_bin)"

dp_write /etc/systemd/system/airflow-webserver.service 0644 <<EOF
[Unit]
Description=Airflow webserver (managed by dpagent)
After=network.target postgresql.service

[Service]
User=airflow
Group=airflow
EnvironmentFile=${ENV_FILE}
ExecStart=${AIRFLOW_BIN} webserver
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

dp_write /etc/systemd/system/airflow-scheduler.service 0644 <<EOF
[Unit]
Description=Airflow scheduler (managed by dpagent)
After=network.target postgresql.service

[Service]
User=airflow
Group=airflow
EnvironmentFile=${ENV_FILE}
EnvironmentFile=-${PIPELINE_SECRETS_FILE}
ExecStart=${AIRFLOW_BIN} scheduler
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

dp_run systemctl daemon-reload
dp_svc_enable airflow-scheduler
dp_svc_enable airflow-webserver

PORT="$(dp_param webserver_port 8090)"
dp_wait_for_port "$PORT" 90

dp_ok "airflow-scheduler and airflow-webserver enabled (port ${PORT})"
