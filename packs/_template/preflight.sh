#!/usr/bin/env bash
# Fail before the install starts, not halfway through it.
#
# Most install failures are predictable: a busy port, no disk, no route to the
# mirror, a conflicting package, SELinux. Checking here costs a second and gives
# the operator an exact instruction. Recovering from the same condition after
# three steps have already run costs far more.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

PORT="$(dp_param port 8080)"
failed=0
note_fail() { dp_err "$*"; failed=1; }

dp_check_root || note_fail "not running as root — use: sudo -E dpagent install CHANGEME"

case "${DP_OS_FAMILY:-}" in
  debian|rhel) ;;
  *) note_fail "unsupported OS family '${DP_OS_FAMILY:-unset}'" ;;
esac

# Always `if A; then B; fi`, never `A && B` as a bare statement: under `set -e`
# a false A makes the whole list non-zero and the script exits.
if dp_port_busy "$PORT"; then
  note_fail "port ${PORT} is already in use; free it or set the port param"
fi

dp_require_disk /var 2048 || failed=1

[ "${DP_SVC_MGR:-}" = "systemd" ] || note_fail "this pack manages a systemd service"

[ "$failed" -eq 0 ] || dp_fail "preflight failed — fix the items above and re-run"
dp_ok "preflight passed"
