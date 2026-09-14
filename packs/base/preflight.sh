#!/usr/bin/env bash
# base runs before everything else, so it cannot assume the tooling the other
# packs' preflights use. Everything here is a shell builtin or coreutils.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

failed=0
note_fail() { dp_err "$*"; failed=1; }

dp_check_root || note_fail "not running as root — use: sudo -E dpagent install base"

case "${DP_OS_FAMILY:-}" in
  debian|rhel) ;;
  *) note_fail "unsupported OS family '${DP_OS_FAMILY:-unset}'" ;;
esac

# A package manager is the one thing that cannot be installed.
if dp_is_debian; then
  command -v apt-get >/dev/null 2>&1 || note_fail "apt-get is missing on a debian-family host"
else
  command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1 || \
    note_fail "neither dnf nor yum is present on a rhel-family host"
fi

free_mb="$(df -Pm /var 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "$free_mb" ] && [ "$free_mb" -lt 512 ]; then
  note_fail "only ${free_mb}MB free on /var; the base tooling needs ~512MB"
fi

# Not a blocker: the packs work under Enforcing too, but every later pack
# writes to non-standard paths (/opt/...) and opens non-standard ports
# without ever setting a file or port context. A denial there surfaces as a
# service that silently fails to start, not as a message from dpagent — so
# flag the mode now, while it is still cheap to check `ausearch`/`journalctl`.
if command -v getenforce >/dev/null 2>&1; then
  selinux_mode="$(getenforce 2>/dev/null || true)"
  if [ "$selinux_mode" = "Enforcing" ]; then
    dp_warn "SELinux is Enforcing — none of these packs set file or port contexts for the non-standard paths/ports they use. If a later step's service fails to start with no clear error, check: ausearch -m avc -ts recent"
  fi
fi

[ "$failed" -eq 0 ] || dp_fail "preflight failed — fix the items above and re-run"
dp_ok "preflight passed"
