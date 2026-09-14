#!/usr/bin/env bash
# Each tool is checked by running it, not by asking whether the package is
# installed — a package can be present and its binary broken or shadowed.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

WANT_LOCALE="$(dp_param locale en_US.UTF-8)"
failed=0

check() {  # check <label> <command...>
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then dp_ok "$label"; else dp_err "$label"; failed=1; fi
}

check "python3 runs"          python3 -c 'import json,sys; json.dumps({})'
check "curl runs"             curl --version
check "openssl runs"          openssl version
check "tar runs"              tar --version
check "ss lists sockets"      ss -lntH
check "fuser is present"      bash -c 'command -v fuser'
check "pgrep is present"      bash -c 'command -v pgrep'
check "CA bundle is usable"   curl -fsS --max-time 15 -o /dev/null https://www.google.com

if locale -a 2>/dev/null | grep -qiE "^(${WANT_LOCALE}|${WANT_LOCALE//UTF-8/utf8})$"; then
  dp_ok "locale ${WANT_LOCALE} available"
else
  dp_err "locale ${WANT_LOCALE} is not available"
  failed=1
fi

if dp_have timedatectl; then
  if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes; then
    dp_ok "clock is synchronised"
  else
    dp_warn "clock is not synchronised — repository signatures may fail"
  fi
fi

[ "$failed" -eq 0 ] || dp_fail "base verify failed"
dp_ok "base tooling verified"
