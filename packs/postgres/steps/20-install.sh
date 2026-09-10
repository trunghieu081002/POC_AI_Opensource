#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/pg-lib.sh"

dp_require_root
VERSION="$(pg_version)"

if dp_is_debian; then
  # Debian's postinst auto-creates a cluster on the default port; the initdb
  # step adopts or recreates it, so nothing is lost by letting it happen.
  dp_pkg_install "postgresql-${VERSION}" "postgresql-client-${VERSION}"

elif dp_is_rhel; then
  dp_pkg_install "postgresql${VERSION}-server" "postgresql${VERSION}-contrib"

else
  dp_fail "unsupported OS family: ${DP_OS_FAMILY:-unset}"
fi

dp_ok "PostgreSQL ${VERSION} packages installed"
