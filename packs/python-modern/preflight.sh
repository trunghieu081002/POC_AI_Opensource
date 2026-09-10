#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

failed=0
note_fail() { dp_err "$*"; failed=1; }

dp_check_root || note_fail "not running as root - use: sudo -E dpagent install python-modern"

if dp_find_python 3 8 3 13 >/dev/null 2>&1; then
  dp_info "already satisfied: $(dp_find_python 3 8 3 13)"
elif dp_is_debian; then
  note_fail "no Python 3.8+ found, and this Debian/Ubuntu release is too old for
      this pack to add one safely (see steps/10-install.sh for why)"
fi

[ "$failed" -eq 0 ] || dp_fail "preflight failed"
dp_ok "preflight passed"
