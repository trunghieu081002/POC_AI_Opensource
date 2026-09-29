#!/usr/bin/env bash
# /etc/default/silo is the package's own EnvironmentFile (see its systemd
# unit, silo-lib.sh's comment) - this pack writes into it, it does not own
# the file's existence. 0600 silo:silo: only the process that needs the
# root credentials (and root) can read them - the same reasoning dbt's own
# profiles.yml got fixed to, the hard way, in an earlier session
# (docs/deploy-log.md, 2026-09-11).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/silo-lib.sh"

dp_require_root
ROOT_USER="$(dp_param root_user silo_admin)"
ROOT_PASSWORD="$(dp_param_required root_password)"
DATA_DIR="$(sl_data_dir)"
PORT="$(dp_param port 9000)"
CONSOLE_PORT="$(dp_param console_port 9001)"

{
  echo "MINIO_ROOT_USER=${ROOT_USER}"
  echo "MINIO_ROOT_PASSWORD=${ROOT_PASSWORD}"
  echo "MINIO_VOLUMES=${DATA_DIR}"
  echo "MINIO_OPTS=--address :${PORT} --console-address :${CONSOLE_PORT}"
} | dp_write "$(sl_env_file)" 0600 silo:silo

dp_ok "silo env file written ($(sl_env_file))"
