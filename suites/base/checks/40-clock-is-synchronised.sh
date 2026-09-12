#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

if [ "$(dp_param install_time_sync 1)" != "1" ]; then
  dp_skip "install_time_sync=false — this pack was not asked to manage the clock"
fi

dp_have timedatectl || dp_skip "timedatectl is not available to query sync status"

SYNCED="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
[ "$SYNCED" = "yes" ] \
  || dp_fail "timedatectl reports NTPSynchronized=${SYNCED}, expected yes — the service may be enabled but not actually synced"

dp_ok "the clock is actually NTP-synchronised, not just running the service"
