#!/usr/bin/env bash
# A thin wrapper, not a raw symlink to the venv's dbt: it also points
# DBT_PROFILES_DIR at the profile this pack generated, so `dbt` just works from
# any shell without the operator having to know or export anything.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/dbt)"
PROJECT_DIR="$(dp_param project_dir /opt/dbt/project)"
PROFILES_DIR="$(dirname "$PROJECT_DIR")/profiles"

dp_write /usr/local/bin/dbt 0755 <<EOF
#!/usr/bin/env bash
export DBT_PROFILES_DIR="\${DBT_PROFILES_DIR:-${PROFILES_DIR}}"
exec "${INSTALL_DIR}/.venv/bin/dbt" "\$@"
EOF

dp_ok "installed /usr/local/bin/dbt (DBT_PROFILES_DIR defaults to ${PROFILES_DIR})"
