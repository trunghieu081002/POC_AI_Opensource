#!/usr/bin/env bash
# Negative: a request signed with the wrong secret must be refused. A store
# that answers anyone makes the shared key decoration.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
# shellcheck source=/dev/null
source "${INSTALL_DIR}/credentials.env"
BUCKET="$(printf '%s' "$NS" | tr '_' '-' | tr '[:upper:]' '[:lower:]')"

if S3_ENDPOINT="$S3_ENDPOINT" S3_ACCESS_KEY="$S3_ACCESS_KEY" S3_SECRET_KEY="wrong-${S3_SECRET_KEY}" \
     python3 "${INSTALL_DIR}/bin/s3.py" list "$BUCKET" >/dev/null 2>&1; then
  dp_fail "a request signed with the wrong secret key was accepted"
fi
if S3_ENDPOINT="$S3_ENDPOINT" S3_ACCESS_KEY="not-${S3_ACCESS_KEY}" S3_SECRET_KEY="$S3_SECRET_KEY" \
     python3 "${INSTALL_DIR}/bin/s3.py" list "$BUCKET" >/dev/null 2>&1; then
  dp_fail "a request with an unknown access key was accepted"
fi
dp_ok "a wrong secret and an unknown access key are both refused"
