#!/usr/bin/env bash
# Catches a whole class of installs that pass every liveness check and still
# lose everything: data_dir on tmpfs, a bind-mount that resets, a container-
# derived image where the data path is an overlay. The component is "up"
# the whole time. Only a restart exposes it.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

BUCKET="$(echo "${DP_TEST_NS:?}" | tr '_' '-')"
export S3_HOST=127.0.0.1
export S3_PORT="$(dp_param port 9000)"
export S3_ACCESS_KEY="$(dp_param root_user silo_admin)"
export S3_SECRET_KEY="$(dp_param_required root_password)"
S3="python3 ${DP_SUITE_ROOT:?}/s3sig.py"

KEY="persist-$(date -u +%s)-$$"
$S3 PUT "$BUCKET" "$KEY" --body "still here after a restart" >/dev/null \
  || dp_fail "PUT failed for ${KEY}"

# Confirm it is visible before touching the service, so a failure below is
# unambiguously about persistence, not a flaky write.
[ "$($S3 GET "$BUCKET" "$KEY")" = "still here after a restart" ] \
  || dp_fail "object not visible immediately after writing it"

dp_run systemctl restart silo
dp_wait_for_port "$S3_PORT" 60

READBACK="$($S3 GET "$BUCKET" "$KEY" 2>&1)" \
  || dp_fail "object gone after restart - data_dir is not actually persistent"
[ "$READBACK" = "still here after a restart" ] \
  || dp_fail "object came back altered after restart"

$S3 DELETE "$BUCKET" "$KEY" >/dev/null || true

dp_ok "committed object survives a service restart"
