#!/usr/bin/env bash
# NEGATIVE CHECK — passes only when the server is *not* reachable where it
# should not be.
#
# `listen_addresses = localhost` is the default in the spec, and an install that
# quietly binds 0.0.0.0 instead exposes the warehouse to the network while every
# health check stays green. Verified by what the kernel reports is bound, not by
# what the config file claims.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/postgres/pg-lib.sh"

PORT="$(dp_param port 5432)"
LISTEN="$(dp_param listen_addresses localhost)"

dp_have ss || dp_skip "ss is not installed; cannot inspect what is bound (install the base pack)"

BOUND="$(ss -lntH 2>/dev/null | awk -v p=":${PORT}\$" '$4 ~ p {print $4}')"
[ -n "$BOUND" ] || dp_fail "nothing is bound to port ${PORT} at all"

dp_info "bound addresses on ${PORT}:"
printf '%s\n' "$BOUND" | sed 's/^/    /' >&2

case "$LISTEN" in
  localhost|127.0.0.1|::1|"127.0.0.1,::1")
    # Every bound address must be loopback. Strip the :port suffix and compare.
    while read -r endpoint; do
      [ -n "$endpoint" ] || continue
      addr="${endpoint%:*}"
      addr="${addr#[}"; addr="${addr%]}"
      case "$addr" in
        127.0.0.1|::1|localhost) ;;
        0.0.0.0|::|*)
          dp_fail "listen_addresses is '${LISTEN}' but the server is bound to ${addr}.
It is reachable from the network. The config was not applied, or something else
is holding the port. Nothing else in this install would have told you." ;;
      esac
    done <<< "$BOUND"
    dp_ok "bound to loopback only, as configured"
    ;;
  *)
    dp_info "listen_addresses is '${LISTEN}' — a network bind is intended here"
    if printf '%s\n' "$BOUND" | grep -qE '(^|\s)(0\.0\.0\.0|\[::\]):'; then
      dp_warn "bound to all interfaces; confirm the firewall and the pg_hba CIDR are narrow"
    fi
    dp_ok "network bind matches the configuration"
    ;;
esac
