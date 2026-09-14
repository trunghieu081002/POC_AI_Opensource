#!/usr/bin/env bash
# Remove everything this pack installed. Must be safe to run against a partial
# install, so every removal is guarded by an existence check.
#
# This DESTROYS the databases. dpagent asks for confirmation before calling it.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/pg-lib.sh"

dp_require_root
VERSION="$(pg_version)"
SERVICE="$(pg_service)"
DATADIR="$(pg_datadir)"
CONFDIR="$(pg_confdir)"

dp_warn "rolling back PostgreSQL ${VERSION} — all data in ${DATADIR} will be lost"

if dp_svc_exists "$SERVICE"; then
  dp_svc_stop "$SERVICE" || dp_warn "could not stop ${SERVICE}; continuing"
fi

if dp_is_debian; then
  if pg_lsclusters -h 2>/dev/null | awk '{print $1"/"$2}' | grep -qx "${VERSION}/main"; then
    dp_run pg_dropcluster --stop "${VERSION}" main
  fi
  dp_pkg_remove "postgresql-${VERSION}" "postgresql-client-${VERSION}"
  # `[ -f x ] && rm x` looks harmless but exits the script under `set -e` when
  # the file is absent — which is the normal case for a partial rollback.
  if [ -f /etc/apt/sources.list.d/pgdg.list ]; then
    dp_run rm -f /etc/apt/sources.list.d/pgdg.list
  fi
  if [ -f /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc ]; then
    dp_run rm -f /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
  fi
else
  dp_pkg_remove "postgresql${VERSION}-server" "postgresql${VERSION}-contrib"
  # 10-repo.sh enables PGDG by installing this actual RPM package (not just
  # dropping a file, unlike the debian branch above) - left in place, a
  # re-install of any postgres version keeps seeing the PGDG repo as already
  # configured, and `dpagent status` shows the package still present even
  # though rollback claims to have removed everything this pack installed.
  if dp_pkg_installed pgdg-redhat-repo; then
    dp_pkg_remove pgdg-redhat-repo
  fi
fi

# Guarded so a bad version param can never turn this into a wildcard delete.
case "$DATADIR" in
  /var/lib/postgresql/*/main|/var/lib/pgsql/*/data)
    if [ -d "$DATADIR" ]; then
      dp_run rm -rf "$DATADIR"
    fi ;;
  *)
    dp_warn "refusing to remove unexpected data directory: ${DATADIR}" ;;
esac

if [ -f "${CONFDIR}/conf.d/10-dpagent.conf" ]; then
  dp_run rm -f "${CONFDIR}/conf.d/10-dpagent.conf"
fi

dp_ok "PostgreSQL ${VERSION} rolled back"
