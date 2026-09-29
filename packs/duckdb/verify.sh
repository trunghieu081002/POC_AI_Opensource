#!/usr/bin/env bash
# Liveness for a CLI tool, not a service: the binary runs, and httpfs loads
# from the pre-fetched local copy with zero network calls - the one thing
# this pack exists to guarantee (INSTALL httpfs at query time hangs on a
# host with broken IPv6, see steps/20-httpfs.sh).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

INSTALL_DIR="$(dp_param install_dir /opt/duckdb)"
BIN="${INSTALL_DIR}/bin/duckdb"
failed=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then dp_ok "$label"; else dp_err "$label"; failed=1; fi
}

check "duckdb binary runs"  "$BIN" --version

if OUT="$("$BIN" -c "SET extension_directory='${INSTALL_DIR}/extensions'; LOAD httpfs; SELECT 42;" 2>&1)"; then
  dp_ok "httpfs loads from the local extension directory"
else
  dp_err "httpfs failed to load from ${INSTALL_DIR}/extensions"
  echo "$OUT" >&2
  failed=1
fi

[ "$failed" -eq 0 ] || dp_fail "duckdb verify failed"
dp_ok "duckdb verified"
