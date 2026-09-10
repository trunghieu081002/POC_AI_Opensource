#!/usr/bin/env bash
# Apply settings through a drop-in file rather than editing postgresql.conf.
# The packaged conf stays pristine, so this step is idempotent by construction
# and `dpagent rollback` has one file to remove.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/pg-lib.sh"

dp_require_root
VERSION="$(pg_version)"
CONFDIR="$(pg_confdir)"
SERVICE="$(pg_service)"
PORT="$(dp_param port 5432)"
LISTEN="$(dp_param listen_addresses localhost)"
MAXCONN="$(dp_param max_connections 100)"
SHARED_BUFFERS="$(dp_param shared_buffers)"

[ -d "$CONFDIR" ] || dp_fail "config directory ${CONFDIR} does not exist — did initdb run?"

CONF_D="${CONFDIR}/conf.d"
DROPIN="${CONF_D}/10-dpagent.conf"

# conf.d exists by default on Debian; on RHEL it must be created and included.
dp_run mkdir -p "$CONF_D"
if ! grep -qE "^\s*include_dir\s*=\s*'conf\.d'" "${CONFDIR}/postgresql.conf" 2>/dev/null; then
  dp_backup "${CONFDIR}/postgresql.conf"
  dp_sh "printf \"\ninclude_dir = 'conf.d'\n\" >> '${CONFDIR}/postgresql.conf'"
fi

{
  echo "# Managed by dpagent — edit the pack params, not this file."
  echo "port = ${PORT}"
  echo "listen_addresses = '${LISTEN}'"
  echo "max_connections = ${MAXCONN}"
  # NOT `[ -n "$X" ] && echo ...`: this group runs as a pipeline component, so a
  # false test would trip `set -e` and truncate everything below it — the config
  # would be written, silently missing half its settings.
  if [ -n "$SHARED_BUFFERS" ]; then
    echo "shared_buffers = '${SHARED_BUFFERS}'"
  fi
  # Must be set before step 50 creates any role, or passwords are stored as md5.
  echo "password_encryption = scram-sha-256"
  echo "logging_collector = on"
  echo "log_line_prefix = '%m [%p] %q%u@%d '"
  echo "log_min_duration_statement = 1000"
} | dp_write "$DROPIN" 0644 postgres:postgres

HBA="${CONFDIR}/pg_hba.conf"

# Harden TCP auth on loopback. RHEL's initdb ships `ident` (and older releases
# `trust`) for 127.0.0.1, which means a server that passes every liveness check
# will happily accept a connection with the wrong password. The postgres
# acceptance suite asserts that it does not, so fix the cause here.
if [ -f "$HBA" ]; then
  if grep -qE '^\s*host\s+all\s+all\s+(127\.0\.0\.1/32|::1/128)\s+(trust|ident|password|md5)\b' "$HBA"; then
    dp_backup "$HBA"
    dp_sh "sed -i -E 's|^([[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+(127\.0\.0\.1/32|::1/128)[[:space:]]+)(trust|ident|password|md5)\b|\1scram-sha-256|' '${HBA}'"
    dp_info "loopback TCP auth set to scram-sha-256"
  fi
fi

# Allow password auth from the network only when actually listening on it.
if [ "$LISTEN" != "localhost" ] && [ "$LISTEN" != "127.0.0.1" ]; then
  if ! grep -q "dpagent-managed" "$HBA" 2>/dev/null; then
    dp_backup "$HBA"
    dp_sh "printf '# dpagent-managed\nhost all all 0.0.0.0/0 scram-sha-256\n' >> '${HBA}'"
    dp_warn "pg_hba now accepts password auth from any host — restrict this CIDR for production"
  fi
  if [ "$(dp_param open_firewall 0)" = "1" ]; then
    dp_firewall_allow "$PORT"
  fi
fi

dp_svc_restart "$SERVICE"
dp_wait_for_port "$PORT" 60

dp_ok "PostgreSQL ${VERSION} listening on ${LISTEN}:${PORT} (max_connections=${MAXCONN})"
