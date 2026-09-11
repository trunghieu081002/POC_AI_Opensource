#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

INSTALL_DIR="$(dp_param install_dir /opt/dbt)"
DBT_VERSION="$(dp_param dbt_version "1.8.*")"
DBT_BIN="${INSTALL_DIR}/.venv/bin/dbt"
failed=0

if [ -x "$DBT_BIN" ]; then
  dp_ok "dbt binary present at ${DBT_BIN}"
else
  dp_err "dbt binary missing at ${DBT_BIN}"
  failed=1
fi

if [ -x /usr/local/bin/dbt ]; then
  dp_ok "/usr/local/bin/dbt wrapper present"
else
  dp_err "/usr/local/bin/dbt wrapper missing"
  failed=1
fi

if [ "$failed" -eq 0 ]; then
  # Newer dbt-core prints a multi-line "Core:\n  - installed: X.Y.Z\n..." block
  # rather than a single "installed version: X.Y.Z" line, so the version is
  # never on line 1 — grep for the line that actually names it.
  version_line="$(/usr/local/bin/dbt --version 2>/dev/null | grep -m1 -i 'installed' || true)"
  dp_info "reported version: ${version_line}"
  case "$version_line" in
    *"${DBT_VERSION%.*}"*) dp_ok "version matches the requested ${DBT_VERSION}" ;;
    *) dp_warn "could not confirm the version string matches ${DBT_VERSION}" ;;
  esac
fi

[ "$failed" -eq 0 ] || dp_fail "dbt verify failed"
dp_ok "dbt verified (liveness only - run 'dbt debug' to check the database connection)"
