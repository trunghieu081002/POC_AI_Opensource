#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/mn-lib.sh"

PORT="$(dp_param port 9000)"
CONSOLE_PORT="$(dp_param console_port 9001)"
DATA_DIR="$(mn_data_dir)"
failed=0
note_fail() { dp_err "$*"; failed=1; }

dp_info "preflight for minio on ${DP_OS_ID:-?} ${DP_OS_VERSION:-?}"

dp_check_root || note_fail "not running as root - use: sudo -E dpagent install minio"

case "${DP_OS_FAMILY:-}" in
  debian|rhel) ;;
  *) note_fail "unsupported OS family '${DP_OS_FAMILY:-unset}'; this pack covers debian and rhel" ;;
esac

ARCH="$(mn_arch)"
[ -n "$ARCH" ] || note_fail "unsupported CPU architecture '$(uname -m)' - MinIO ships amd64 and arm64 builds only"

if [ "$PORT" = "$CONSOLE_PORT" ]; then
  note_fail "port and console_port must differ (both are ${PORT})"
fi
if dp_port_busy "$PORT"; then
  note_fail "port ${PORT} (S3 API) is already in use; free it or set the port param"
fi
if dp_port_busy "$CONSOLE_PORT"; then
  note_fail "console_port ${CONSOLE_PORT} is already in use; free it or set the console_port param"
fi

datadir_parent="$(dirname "$DATA_DIR")"
check_path="/opt"
if [ -d "$datadir_parent" ]; then
  check_path="$datadir_parent"
fi
dp_require_disk "$check_path" 2048 || failed=1

if [ "$(dp_param open_firewall 0)" != "1" ] && [ "${DP_FIREWALL:-none}" != "none" ]; then
  dp_warn "a ${DP_FIREWALL} firewall is active and open_firewall is off - minio will only be reachable from this host until it is opened (see postgres/airflow's own preflight for the same gap, found for real on a firewalld host)"
fi

if dp_have curl; then
  if ! curl -fsS --max-time 15 -o /dev/null "https://dl.min.io" 2>/dev/null; then
    note_fail "cannot reach https://dl.min.io - check DNS, egress firewall, or export http(s)_proxy and re-run with sudo -E"
  fi
fi

[ "${DP_SVC_MGR:-}" = "systemd" ] || note_fail "this pack manages the service through systemd, which is not present"

[ "$failed" -eq 0 ] || dp_fail "preflight failed - fix the items above and re-run; completed steps are skipped"
dp_ok "preflight passed"
