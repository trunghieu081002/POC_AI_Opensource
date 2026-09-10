#!/usr/bin/env bash
# A wrong clock makes repository signatures and TLS certificates look invalid,
# and the resulting errors say nothing about time. Marked optional in the
# manifest: a host with no NTP egress should warn, not block the install.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root

if dp_have timedatectl && timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes; then
  dp_skip "clock is already synchronised"
fi

if dp_is_debian; then
  if ! dp_svc_exists systemd-timesyncd && ! dp_have chronyd; then
    dp_pkg_install systemd-timesyncd
  fi
  if dp_svc_exists systemd-timesyncd; then
    dp_svc_enable systemd-timesyncd
  fi
else
  dp_have chronyd || dp_pkg_install chrony
  if dp_svc_exists chronyd; then
    dp_svc_enable chronyd
  fi
fi

# `A && B` as a bare statement is a trap under `set -e`: when A is false the
# whole list returns non-zero and the script exits. Always use an if.
if dp_have timedatectl; then
  dp_run timedatectl set-ntp true
fi

if [ "$DP_DRY_RUN" != "1" ]; then
  for _ in $(seq 1 15); do
    if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes; then
      break
    fi
    sleep 2
  done
  if ! timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes; then
    dp_warn "NTP is enabled but not synchronised yet — if repo signatures fail later, check the clock"
  fi
fi

dp_ok "clock synchronisation enabled"
