#!/usr/bin/env bash
# Catches a whole class of installs that pass every liveness check and still
# lose everything: a data directory on tmpfs, a cluster initialised somewhere
# other than where the unit points, fsync disabled, or a container-derived image
# where /var/lib is an overlay that resets.
#
# The component is "up" the whole time. Only a restart exposes it.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/postgres/pg-lib.sh"

NS="${DP_TEST_NS:?}"
PORT="$(dp_param port 5432)"
SERVICE="$(pg_service)"

psql_ns() { runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAX -p "$PORT" -d "$NS" "$@"; }

MARKER="persist-$(date -u +%s)-$$"

psql_ns -c "
CREATE TABLE IF NOT EXISTS dpagent_persistence (
  marker text PRIMARY KEY,
  at     timestamptz NOT NULL DEFAULT now()
);" >/dev/null

psql_ns -c "INSERT INTO dpagent_persistence (marker) VALUES ('${MARKER}');" >/dev/null

# Confirm it is committed and visible before touching the service, so a failure
# after the restart can only mean the restart lost it.
BEFORE="$(psql_ns -c "SELECT count(*) FROM dpagent_persistence WHERE marker = '${MARKER}';")"
[ "$BEFORE" = "1" ] || dp_fail "row was not visible even before the restart"

DATADIR="$(pg_query 'SHOW data_directory;')"
dp_info "data directory in use: ${DATADIR}"
FSTYPE="$(df -PT "$DATADIR" 2>/dev/null | awk 'NR==2 {print $2}')"
case "$FSTYPE" in
  tmpfs|ramfs)
    dp_fail "data directory ${DATADIR} is on ${FSTYPE} — every byte is lost on reboot" ;;
  "") dp_warn "could not determine the filesystem under ${DATADIR}" ;;
  *)  dp_info "filesystem: ${FSTYPE}" ;;
esac

dp_info "restarting ${SERVICE}"
dp_run systemctl restart "$SERVICE"
dp_wait_for_port "$PORT" 90

AFTER="$(psql_ns -c "SELECT count(*) FROM dpagent_persistence WHERE marker = '${MARKER}';")"
[ "$AFTER" = "1" ] || dp_fail "the row survived the commit but not the restart — data is not durable"

dp_ok "committed data survived a full service restart"
