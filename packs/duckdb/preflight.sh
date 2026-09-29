#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

INSTALL_DIR="$(dp_param install_dir /opt/duckdb)"
failed=0
note_fail() { dp_err "$*"; failed=1; }

dp_info "preflight for duckdb on ${DP_OS_ID:-?} ${DP_OS_VERSION:-?}"

dp_check_root || note_fail "not running as root - use: sudo -E dpagent install duckdb"

case "${DP_OS_FAMILY:-}" in
  debian|rhel) ;;
  *) note_fail "unsupported OS family '${DP_OS_FAMILY:-unset}'; this pack covers debian and rhel" ;;
esac

case "$(uname -m)" in
  x86_64|aarch64|arm64) ;;
  *) note_fail "unsupported CPU architecture '$(uname -m)' - duckdb ships amd64 and arm64 builds only" ;;
esac

if ! dp_have python3; then
  note_fail "no python3 found - the base pack should have provided one (needed to extract the .zip)"
fi

dp_require_disk "$(dirname "$INSTALL_DIR")" 512 || failed=1

if dp_have curl; then
  if ! curl -fsS --max-time 15 -o /dev/null "https://github.com" 2>/dev/null; then
    note_fail "cannot reach https://github.com - check DNS, egress firewall, or export http(s)_proxy and re-run with sudo -E"
  fi
  if ! curl -fsS --max-time 15 -o /dev/null "https://extensions.duckdb.org" 2>/dev/null; then
    note_fail "cannot reach https://extensions.duckdb.org - check DNS, egress firewall, or export http(s)_proxy and re-run with sudo -E"
  fi
fi

[ "$failed" -eq 0 ] || dp_fail "preflight failed - fix the items above and re-run; completed steps are skipped"
dp_ok "preflight passed"
