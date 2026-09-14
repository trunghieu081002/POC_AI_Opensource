#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/dbt)"

# A shared group other packs can join to read what this pack writes (the
# profile that holds the warehouse password, the project dbt runs against)
# without making any of it world-readable. Airflow is the intended consumer -
# orchestrating dbt is this stack's whole point - but dbt makes no assumption
# about who joins; see packs/airflow/steps/10-user.sh, which does the same
# call from its side. dbt has no `requires` on airflow (nor the reverse), so
# either pack can install first - whichever runs second is the one for which
# this call actually does something; the other's is a no-op because its peer
# user/group does not exist yet.
dp_ensure_group dbtread
dp_join_group airflow dbtread

# dbt-core shells out to git for `dbt deps` (installing packages from
# packages.yml) and dbt debug reports its absence as a failed check even
# when the actual database connection is fine - so a "successful" install
# that never provisioned git looks broken the moment an operator adds any
# package dependency. Same package name on both families.
dp_have git || dp_pkg_install git

PYTHON="$(dp_find_python 3 8 3 12 || true)"
if [ "$DP_DRY_RUN" != "1" ]; then
  [ -n "$PYTHON" ] || dp_fail "no python 3.8-3.12 on PATH (python-modern should have provided one)"
fi

dp_run mkdir -p "$INSTALL_DIR"
dp_run "$PYTHON" -m venv "${INSTALL_DIR}/.venv"
dp_run "${INSTALL_DIR}/.venv/bin/pip" install --quiet --upgrade pip wheel

dp_ok "venv created at ${INSTALL_DIR}/.venv using ${PYTHON}"
