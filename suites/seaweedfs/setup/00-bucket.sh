#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
# secret params are masked in dpagent's state; the pack's config step wrote the real ones here
# shellcheck source=/dev/null
source "${INSTALL_DIR}/credentials.env"
export S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY

BUCKET="$(printf '%s' "$NS" | tr '_' '-' | tr '[:upper:]' '[:lower:]')"
dp_run python3 "${INSTALL_DIR}/bin/s3.py" create-bucket "$BUCKET"
dp_run python3 "${INSTALL_DIR}/bin/s3.py" head-bucket "$BUCKET"
dp_ok "bucket ${BUCKET} ready"
