#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
PORT="$(dp_param s3_port 8333)"
failed=0

if dp_svc_active seaweedfs; then dp_ok "service seaweedfs is active"; else dp_err "service seaweedfs is not active"; failed=1; fi
if dp_port_busy "$PORT"; then dp_ok "something is listening on ${PORT}"; else dp_err "nothing is listening on ${PORT}"; failed=1; fi

if [ "$failed" -eq 0 ]; then
  # shellcheck source=/dev/null
  source "${INSTALL_DIR}/credentials.env"
  export S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY
  if python3 "${INSTALL_DIR}/bin/s3.py" list-buckets >/dev/null 2>&1; then
    dp_ok "the S3 API answers a signed ListBuckets with the configured key"
  else
    dp_err "the S3 API did not accept a signed ListBuckets with the configured key"; failed=1
  fi
fi
[ "$failed" -eq 0 ] || dp_fail "seaweedfs verify failed"
dp_ok "seaweedfs verified (liveness only - dpagent's acceptance suite proves objects round-trip)"
