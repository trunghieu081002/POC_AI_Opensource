#!/usr/bin/env bash
# NEGATIVE CHECK — this passes only when something is refused.
#
# The single most dangerous false pass: a database that is up, listening,
# answering queries, verified green, and accepting anyone who connects. Every
# liveness check passes on a server configured with `trust`. Only asking it to
# refuse a wrong password reveals it.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/postgres/pg-lib.sh"

NS="${DP_TEST_NS:?}"
PORT="$(dp_param port 5432)"
ROLE="dpagent_selftest_role"
GOOD="correct-horse-$$-$(date -u +%s)"
BAD="definitely-not-the-password"

psql_super() { runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAX -p "$PORT" -d postgres "$@"; }

# Recreate the role so the password is known to this run.
psql_super -c "DROP ROLE IF EXISTS ${ROLE};" >/dev/null
psql_super -c "CREATE ROLE ${ROLE} LOGIN PASSWORD \$pw\$${GOOD}\$pw\$;" >/dev/null
psql_super -c "GRANT CONNECT ON DATABASE ${NS} TO ${ROLE};" >/dev/null

try_connect() {  # try_connect <password> ; returns psql's exit status
  runuser -u postgres -- env PGPASSWORD="$1" PGCONNECT_TIMEOUT=10 \
    psql -h 127.0.0.1 -p "$PORT" -U "$ROLE" -d "$NS" -tAX -c 'SELECT 1' \
    >/dev/null 2>&1
}

# The control: the right password must work. Without this, a server that
# refuses *everything* would pass the negative check and look secure.
if ! try_connect "$GOOD"; then
  dp_fail "the correct password was refused over TCP — password auth is not usable at all.
Check pg_hba.conf: the host line for 127.0.0.1 should be scram-sha-256."
fi
dp_ok "correct password is accepted"

# The actual assertion.
if try_connect "$BAD"; then
  dp_fail "A WRONG PASSWORD WAS ACCEPTED.
The server is authenticating with 'trust' on TCP, so anyone who can reach port
${PORT} is a superuser-adjacent client. Everything else about this install looks
healthy, which is precisely why this check exists.
Fix: set the 127.0.0.1 host line in pg_hba.conf to scram-sha-256 and reload."
fi
dp_ok "wrong password is refused"

# An unknown role must also be refused, not silently mapped to something.
if runuser -u postgres -- env PGPASSWORD="$BAD" PGCONNECT_TIMEOUT=10 \
     psql -h 127.0.0.1 -p "$PORT" -U "no_such_role_$$" -d "$NS" -tAX -c 'SELECT 1' \
     >/dev/null 2>&1; then
  dp_fail "a connection as a non-existent role succeeded — authentication is not enforced"
fi
dp_ok "unknown role is refused"

dp_ok "authentication actually authenticates"
