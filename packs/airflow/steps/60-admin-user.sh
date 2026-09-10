#!/usr/bin/env bash
# Idempotent by checking first: `airflow users create` errors on a duplicate,
# which would otherwise turn a harmless re-run into a failed step.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/af-lib.sh"

dp_require_root
ADMIN_USER="$(dp_param admin_user admin)"
ADMIN_PASSWORD="$(dp_param_required admin_password)"
ADMIN_EMAIL="$(dp_param admin_email admin@example.com)"

if [ "$DP_DRY_RUN" = "1" ]; then
  dp_info "DRY: would ensure admin user '${ADMIN_USER}' exists"
  exit 0
fi

existing="$(af_query users list 2>/dev/null | awk -v u="$ADMIN_USER" '$1 == u {print $1}')"
if [ -n "$existing" ]; then
  dp_skip "admin user '${ADMIN_USER}' already exists"
fi

af_run users create \
  --username "$ADMIN_USER" \
  --firstname dpagent \
  --lastname admin \
  --role Admin \
  --email "$ADMIN_EMAIL" \
  --password "$ADMIN_PASSWORD"

dp_ok "admin user '${ADMIN_USER}' created"
