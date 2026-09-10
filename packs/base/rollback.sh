#!/usr/bin/env bash
# Deliberately does almost nothing, and says why.
#
# Removing curl, python3, iproute or the CA bundle from a running host is a far
# worse outcome than leaving them installed — other software depends on them,
# dpagent itself is written in Python, and on some distros pulling `iproute`
# takes half the system with it. A rollback that would break the host is not a
# rollback.
#
# The manifest requires a rollback script for every pack. This is the honest one.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_warn "base installs shared system tooling (python3, curl, iproute, CA certs)."
dp_warn "Removing it would break unrelated software and dpagent itself, so this"
dp_warn "rollback leaves the packages in place."

# The one thing that is genuinely ours to undo: the NTP service, if we enabled it
# and nothing else is relying on it. Even that only gets disabled, not removed.
if [ "$(dp_param install_time_sync 1)" = "1" ]; then
  dp_info "leaving clock synchronisation enabled; disable it by hand if unwanted"
fi

dp_ok "base rollback complete (no packages removed, by design)"
