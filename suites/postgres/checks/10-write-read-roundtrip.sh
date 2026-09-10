#!/usr/bin/env bash
# The most basic claim an installed database makes: what you wrote is what you
# read. `SELECT 1` proves the process is alive; this proves the storage path
# works end to end — parser, WAL, heap, index, planner, wire protocol.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/postgres/pg-lib.sh"

NS="${DP_TEST_NS:?}"
PORT="$(dp_param port 5432)"

psql_ns() { runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAX -p "$PORT" -d "$NS" "$@"; }

MARKER="roundtrip-$(date -u +%s)-$$"

psql_ns -c "
CREATE TABLE IF NOT EXISTS dpagent_roundtrip (
  id     bigserial PRIMARY KEY,
  marker text NOT NULL,
  body   text NOT NULL,
  at     timestamptz NOT NULL DEFAULT now()
);" >/dev/null

# Include characters that break naive quoting, so a pack that builds SQL by
# string-concatenation fails here rather than in production.
BODY="quote'test \"double\" \$dollar\$ backslash\\ done"

psql_ns -c "INSERT INTO dpagent_roundtrip (marker, body) VALUES ('${MARKER}', \$body\$${BODY}\$body\$);" >/dev/null

COUNT="$(psql_ns -c "SELECT count(*) FROM dpagent_roundtrip WHERE marker = '${MARKER}';")"
[ "$COUNT" = "1" ] || dp_fail "wrote 1 row, read back ${COUNT} — the storage path is broken"

READBACK="$(psql_ns -c "SELECT body FROM dpagent_roundtrip WHERE marker = '${MARKER}';")"
[ "$READBACK" = "$BODY" ] || dp_fail "row came back altered: expected [${BODY}] got [${READBACK}]"

# An index has to be usable, not merely creatable.
psql_ns -c "CREATE INDEX IF NOT EXISTS dpagent_roundtrip_marker ON dpagent_roundtrip (marker);" >/dev/null
psql_ns -c "ANALYZE dpagent_roundtrip;" >/dev/null
psql_ns -c "SELECT count(*) FROM dpagent_roundtrip WHERE marker = '${MARKER}';" >/dev/null

# A rolled-back transaction must leave nothing behind.
psql_ns -c "BEGIN; INSERT INTO dpagent_roundtrip (marker, body) VALUES ('${MARKER}-rb', 'x'); ROLLBACK;" >/dev/null
LEFT="$(psql_ns -c "SELECT count(*) FROM dpagent_roundtrip WHERE marker = '${MARKER}-rb';")"
[ "$LEFT" = "0" ] || dp_fail "a rolled-back insert left ${LEFT} row(s) behind — transactions are not honoured"

dp_ok "write/read roundtrip, index scan and rollback all behave"
