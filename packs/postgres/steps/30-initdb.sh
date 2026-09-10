#!/usr/bin/env bash
# Initialise the data directory and bring the service up.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/pg-lib.sh"

dp_require_root
VERSION="$(pg_version)"
DATADIR="$(pg_datadir)"
SERVICE="$(pg_service)"

if [ -s "${DATADIR}/PG_VERSION" ]; then
  dp_info "data directory already initialised at ${DATADIR}"
else
  if dp_is_debian; then
    # The package postinst usually created 'main' already. Only build one if not.
    if pg_lsclusters -h 2>/dev/null | awk '{print $1"/"$2}' | grep -qx "${VERSION}/main"; then
      dp_info "cluster ${VERSION}/main already exists"
    else
      dp_run pg_createcluster --start "${VERSION}" main -- \
        --encoding=UTF8 --locale=C.UTF-8
    fi
  else
    dp_info "running initdb for PostgreSQL ${VERSION}"
    dp_run "/usr/pgsql-${VERSION}/bin/postgresql-${VERSION}-setup" initdb
  fi
fi

dp_svc_enable "$SERVICE"

# Deliberately not waiting on the target port here: the port param is applied by
# the config step that runs next. Wait on the service instead, and let config
# confirm the final port once it has restarted.
if [ "$DP_DRY_RUN" != "1" ]; then
  for _ in $(seq 1 60); do
    # `A && break` would exit the whole script under `set -e` on the first
    # iteration where the service is not up yet — i.e. always.
    if dp_svc_active "$SERVICE"; then
      break
    fi
    sleep 1
  done
  dp_svc_active "$SERVICE" || dp_fail "service ${SERVICE} did not become active"
fi

dp_ok "PostgreSQL ${VERSION} is running (service ${SERVICE})"
