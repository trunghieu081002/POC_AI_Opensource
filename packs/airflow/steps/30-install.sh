#!/usr/bin/env bash
# Installs with Airflow's own published constraints file, which is the
# officially documented way to get a reproducible set of dependency versions -
# without it, pip's normal resolver frequently produces a broken combination.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

dp_require_root
VERSION="$(dp_param version 2.9.3)"
PIP="$(af_venv)/bin/pip"

[ -x "$PIP" ] || dp_fail "venv missing at $(af_venv) - the venv step should have created it"

PYVER="$("$(af_venv)/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
CONSTRAINTS_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${VERSION}/constraints-${PYVER}.txt"

dp_info "installing apache-airflow==${VERSION} for python ${PYVER}"
dp_info "constraints: ${CONSTRAINTS_URL}"

dp_run "$PIP" install --quiet \
  "apache-airflow[postgres]==${VERSION}" \
  --constraint "$CONSTRAINTS_URL"

INSTALLED="$("$(af_venv)/bin/airflow" version 2>/dev/null || true)"
dp_ok "installed: airflow ${INSTALLED:-$VERSION}"
