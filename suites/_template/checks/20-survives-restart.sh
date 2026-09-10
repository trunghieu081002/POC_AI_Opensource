#!/usr/bin/env bash
# Committed data is still there after a service restart.
#
# Catches a whole class of installs that pass every liveness check and still
# lose everything: a data directory on tmpfs, fsync disabled, a unit pointing at
# a different path than the one that was initialised. The component is "up" the
# entire time — only a restart exposes it.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
SERVICE="CHANGEME"
PORT="$(dp_param port 8080)"
MARKER="persist-$(date -u +%s)-$$"

# 1. write $MARKER

# 2. confirm it is visible BEFORE the restart, so a failure afterwards can only
#    mean the restart lost it
BEFORE=0
[ "$BEFORE" = "1" ] || dp_fail "the value was not visible even before the restart"

# 3. the cheap check that would have caught it without a restart
DATADIR="/var/lib/CHANGEME"
FSTYPE="$(df -PT "$DATADIR" 2>/dev/null | awk 'NR==2 {print $2}')"
case "$FSTYPE" in
  tmpfs|ramfs) dp_fail "data directory ${DATADIR} is on ${FSTYPE} — lost on reboot" ;;
  "")          dp_warn "could not determine the filesystem under ${DATADIR}" ;;
  *)           dp_info "filesystem: ${FSTYPE}" ;;
esac

dp_run systemctl restart "$SERVICE"
dp_wait_for_port "$PORT" 90

# 4. read $MARKER back
AFTER=0
[ "$AFTER" = "1" ] || dp_fail "the value survived the commit but not the restart"

dp_ok "committed data survived a full service restart"
