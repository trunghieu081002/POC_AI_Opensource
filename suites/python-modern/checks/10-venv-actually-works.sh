#!/usr/bin/env bash
# dbt and airflow both exist to do exactly this: `python -m venv` with this
# interpreter, then use pip inside it. verify.sh only proves the base
# interpreter can import the right stdlib modules directly - this proves the
# venv machinery itself (ensurepip, pip's own bootstrap) actually works,
# which is the real, specific thing every consumer of this pack depends on.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
VENV_DIR="/tmp/${NS}-venv"
rm -rf "$VENV_DIR"

FOUND="$(dp_find_python 3 8 3 13 || true)"
[ -n "$FOUND" ] || dp_fail "no python 3.8-3.13 found on PATH"

"$FOUND" -m venv "$VENV_DIR" || dp_fail "${FOUND} -m venv failed to create an environment"

"${VENV_DIR}/bin/pip" --version >/dev/null 2>&1 \
  || dp_fail "the venv's own pip does not run — ensurepip likely failed silently during venv creation"

"${VENV_DIR}/bin/python" -c "import ssl, sqlite3, json, venv" \
  || dp_fail "the venv's interpreter is missing a stdlib module dbt/airflow need"

rm -rf "$VENV_DIR"
dp_ok "a real venv from ${FOUND} has working pip and the stdlib modules dbt/airflow need"
