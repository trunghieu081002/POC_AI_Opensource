#!/usr/bin/env bash
# Undo the install completely.
#
# Must be safe against a half-finished install, so guard every removal with an
# existence check. Never let a parameter expand into a path you did not intend —
# match the shape of the path before deleting it.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
VERSION="$(dp_param version 1.0)"
DATADIR="/var/lib/CHANGEME"

dp_warn "rolling back CHANGEME ${VERSION} — data in ${DATADIR} will be lost"

# `[ -f x ] && rm x` exits the script under `set -e` when x is absent — which is
# the normal case here, since a rollback often runs against a partial install.
# Use an if for every guarded action.
if dp_svc_exists CHANGEME; then
  dp_svc_stop CHANGEME || dp_warn "could not stop service"
fi

dp_pkg_remove some-package

case "$DATADIR" in
  /var/lib/CHANGEME*)
    if [ -d "$DATADIR" ]; then
      dp_run rm -rf "$DATADIR"
    fi ;;
  *) dp_warn "refusing to remove unexpected path: ${DATADIR}" ;;
esac

if [ -f /etc/CHANGEME/CHANGEME.conf ]; then
  dp_run rm -f /etc/CHANGEME/CHANGEME.conf
fi

dp_ok "CHANGEME rolled back"
