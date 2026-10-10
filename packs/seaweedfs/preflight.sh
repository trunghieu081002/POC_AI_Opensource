#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

failed=0
note_fail() { dp_err "$*"; failed=1; }

dp_check_root || note_fail "not running as root - use: sudo -E dpagent install seaweedfs"

case "$(uname -m)" in
  x86_64|amd64|aarch64|arm64) ;;
  *) note_fail "unsupported CPU architecture $(uname -m) - the release tarballs this pack pins exist for amd64 and arm64 only" ;;
esac

if ! dp_have systemctl; then
  note_fail "systemd is required (the unit is managed with systemctl)"
fi

PORT="$(dp_param s3_port 8333)"
if dp_svc_exists seaweedfs 2>/dev/null && dp_svc_active seaweedfs; then
  dp_info "seaweedfs is already running - a re-run will reconfigure it"
elif dp_port_busy "$PORT"; then
  note_fail "port ${PORT} is already in use by something else - pick another with --set s3_port=..."
fi

DATA_DIR="$(dp_param data_dir /var/lib/seaweedfs)"
dp_require_disk "$(dirname "$DATA_DIR")" 2048 || failed=1

if dp_have curl; then
  if ! curl -fsI --max-time 10 https://github.com >/dev/null 2>&1; then
    dp_warn "cannot reach github.com right now - the release download will fail unless it becomes reachable"
  fi
fi

[ "$failed" -eq 0 ] || dp_fail "preflight failed"
dp_ok "preflight passed"
