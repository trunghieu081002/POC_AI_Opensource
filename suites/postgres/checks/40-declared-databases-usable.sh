#!/usr/bin/env bash
# The pack's verify.sh asks whether each database exists in pg_database. That is
# a row in a catalog, not a working database — one can exist and still refuse
# connections, have no usable schema, or sit on a tablespace that is gone.
#
# This connects to each one and writes to it.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACKS_DIR:?}/postgres/pg-lib.sh"

PORT="$(dp_param port 5432)"
failed=0
checked=0

while read -r db; do
  [ -n "$db" ] || continue
  checked=$((checked + 1))

  if ! runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAX -p "$PORT" -d "$db" \
        -c 'SELECT 1' >/dev/null 2>&1; then
    dp_err "${db}: exists in the catalog but refuses a connection"
    failed=1
    continue
  fi

  # Write into a temporary table: proves the database is writable without
  # leaving anything behind in a database the operator actually uses.
  if ! runuser -u postgres -- psql -v ON_ERROR_STOP=1 -tAX -p "$PORT" -d "$db" -c "
        CREATE TEMP TABLE dpagent_probe (v int);
        INSERT INTO dpagent_probe VALUES (1);
        SELECT count(*) FROM dpagent_probe;" >/dev/null 2>&1; then
    dp_err "${db}: connects but will not accept a write (read-only? out of disk? no temp space?)"
    failed=1
    continue
  fi

  encoding="$(runuser -u postgres -- psql -tAX -p "$PORT" -d "$db" \
      -c 'SHOW server_encoding;' 2>/dev/null || true)"
  if [ "$encoding" != "UTF8" ]; then
    dp_err "${db}: server_encoding is '${encoding}', not UTF8 — non-ASCII data will corrupt"
    failed=1
    continue
  fi

  dp_ok "${db}: connects, writes, UTF8"
done < <(dp_json_list databases)

if [ "$checked" -eq 0 ]; then
  dp_skip "the spec declared no databases"
fi

[ "$failed" -eq 0 ] || dp_fail "one or more declared databases are not actually usable"
dp_ok "all ${checked} declared database(s) are usable"
