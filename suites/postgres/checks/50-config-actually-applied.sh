#!/usr/bin/env bash
# A config file that contains a setting and a server that is running that setting
# are different things. A drop-in can be written to a conf.d that is never
# included, or overridden later in the file, and nothing complains.
#
# Everything here is read back from the running server.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/postgres/pg-lib.sh"

WANT_PORT="$(dp_param port 5432)"
WANT_MAXCONN="$(dp_param max_connections 100)"
WANT_VERSION="$(pg_version)"
failed=0

expect() {  # expect <setting> <wanted> <explanation>
  local setting="$1" wanted="$2" why="$3" actual
  actual="$(pg_query "SHOW ${setting};" || true)"
  if [ "$actual" = "$wanted" ]; then
    dp_ok "${setting} = ${actual}"
  else
    dp_err "${setting} is '${actual}', the spec asked for '${wanted}' — ${why}"
    failed=1
  fi
}

expect port "$WANT_PORT" "the drop-in was not applied, or another config sets it later"
expect max_connections "$WANT_MAXCONN" "the drop-in was not applied"

running_version="$(pg_query 'SHOW server_version;' || true)"
case "$running_version" in
  "${WANT_VERSION}."*|"${WANT_VERSION}")
    dp_ok "server_version = ${running_version}" ;;
  *)
    dp_err "running PostgreSQL ${running_version}, the spec asked for ${WANT_VERSION} — \
an older cluster is probably still bound to this port"
    failed=1 ;;
esac

# The drop-in has to be in the include path, or the next setting added to it
# will silently do nothing.
conf_file="$(pg_query 'SHOW config_file;' || true)"
dp_info "config_file = ${conf_file}"
sources="$(pg_query "SELECT DISTINCT sourcefile FROM pg_settings WHERE sourcefile IS NOT NULL;" || true)"
if printf '%s\n' "$sources" | grep -q '10-dpagent.conf'; then
  dp_ok "the dpagent drop-in is in the active include path"
else
  dp_err "no setting in the running server comes from 10-dpagent.conf — \
the drop-in exists but is not being read"
  failed=1
fi

# Passwords must not be stored with md5.
pw_enc="$(pg_query 'SHOW password_encryption;' || true)"
if [ "$pw_enc" = "scram-sha-256" ]; then
  dp_ok "password_encryption = scram-sha-256"
else
  dp_err "password_encryption is '${pw_enc}' — new passwords will be stored weakly"
  failed=1
fi

[ "$failed" -eq 0 ] || dp_fail "the running server does not match the spec"
dp_ok "the running server matches the spec"
