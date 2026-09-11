#!/usr/bin/env bash
# Enable the PGDG repository. The distro's own postgres package lags badly and
# pins you to whatever major that release shipped, so we take it from upstream.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/pg-lib.sh"

dp_require_root

if dp_is_debian; then
  dp_pkg_update
  dp_pkg_install curl ca-certificates gnupg lsb-release

  keyring=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  dp_run install -d /usr/share/postgresql-common/pgdg
  dp_fetch "https://www.postgresql.org/media/keys/ACCC4CF8.asc" "$keyring"

  codename="$(lsb_release -cs)"
  dp_info "adding PGDG for ${codename}"
  dp_write /etc/apt/sources.list.d/pgdg.list 0644 <<EOF
deb [signed-by=${keyring}] https://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main
EOF

  dp_pkg_update

elif dp_is_rhel; then
  el="$(rpm -E %rhel)"
  arch="$(uname -m)"
  dp_info "adding PGDG for EL${el} ${arch}"
  dp_run "${DP_PKG_MGR:-dnf}" install -y \
    "https://download.postgresql.org/pub/repos/yum/reporpms/EL-${el}-${arch}/pgdg-redhat-repo-latest.noarch.rpm"

  # The distro module stream shadows the PGDG packages if left enabled. -y is
  # required here, not just on the disable itself: listing a module from a
  # just-added repo triggers that repo's first-use GPG key confirmation, which
  # fails non-interactively without it — silently skipping the disable below
  # and leaving postgresqlNN-server unresolvable ("dnf-no-match").
  if "${DP_PKG_MGR:-dnf}" -y module list postgresql >/dev/null 2>&1; then
    dp_run "${DP_PKG_MGR:-dnf}" -qy module disable postgresql
  fi

else
  dp_fail "unsupported OS family: ${DP_OS_FAMILY:-unset}"
fi

dp_ok "PGDG repository enabled"
