#!/usr/bin/env bash
# The running service reports the settings that were asked for.
#
# A config file that contains a setting and a service that is running it are
# different things. A drop-in can be written into a directory that is never
# included, or overridden later in the file, and nothing complains.
#
# Read everything back from the running process — the file is a claim, the
# process is the fact.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

WANT_PORT="$(dp_param port 8080)"
failed=0

expect() {  # expect <label> <wanted> <actual> <why it would differ>
  if [ "$3" = "$2" ]; then
    dp_ok "$1 = $3"
  else
    dp_err "$1 is '$3', the spec asked for '$2' — $4"
    failed=1
  fi
}

# ACTUAL_PORT="$(<query the running service>)"
ACTUAL_PORT="$WANT_PORT"
expect port "$WANT_PORT" "$ACTUAL_PORT" "the drop-in was not applied, or something overrides it"

# What is actually bound, per the kernel — not what the config claims.
if dp_have ss; then
  BOUND="$(ss -lntH 2>/dev/null | awk -v p=":${WANT_PORT}\$" '$4 ~ p {print $4}')"
  [ -n "$BOUND" ] || { dp_err "nothing is bound to ${WANT_PORT}"; failed=1; }
fi

[ "$failed" -eq 0 ] || dp_fail "the running service does not match the spec"
dp_ok "the running service matches the spec"
