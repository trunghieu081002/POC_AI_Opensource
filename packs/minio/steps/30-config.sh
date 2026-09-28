#!/usr/bin/env bash
# The root credentials and server flags live in one env file the systemd unit
# reads - never in the unit file itself, which `systemctl cat`/`systemctl
# show` print to anyone who can run them. 0600 minio:minio: only the process
# that needs the credentials (and root) can read them - the same reasoning
# dbt's own profiles.yml got fixed to, the hard way, in an earlier session
# (docs/deploy-log.md, 2026-09-11).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/mn-lib.sh"

dp_require_root
ROOT_USER="$(dp_param root_user minio_admin)"
ROOT_PASSWORD="$(dp_param_required root_password)"
DATA_DIR="$(mn_data_dir)"
PORT="$(dp_param port 9000)"
CONSOLE_PORT="$(dp_param console_port 9001)"

{
  echo "MINIO_ROOT_USER=${ROOT_USER}"
  echo "MINIO_ROOT_PASSWORD=${ROOT_PASSWORD}"
  echo "MINIO_VOLUMES=${DATA_DIR}"
  echo "MINIO_OPTS=--address :${PORT} --console-address :${CONSOLE_PORT}"
} | dp_write "$(mn_env_file)" 0600 minio:minio

dp_ok "minio env file written ($(mn_env_file))"
