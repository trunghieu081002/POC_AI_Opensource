#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
# shellcheck source=/dev/null
source "${INSTALL_DIR}/credentials.env"
export S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY
BUCKET="$(printf '%s' "$NS" | tr '_' '-' | tr '[:upper:]' '[:lower:]')"
S3="python3 ${INSTALL_DIR}/bin/s3.py"

# Empty it, then delete it: an empty bucket still holds volume slots in its
# collection, and a suite that leaked one per run would eventually starve the
# very pipelines the store is for.
while read -r key; do
  [ -n "$key" ] || continue
  $S3 delete "$BUCKET" "$key" || true
done < <($S3 list "$BUCKET" "" 2>/dev/null || true)
LEFT="$($S3 list "$BUCKET" "" 2>/dev/null | grep -c . || true)"
[ "${LEFT:-0}" = 0 ] || dp_fail "${LEFT} object(s) left in throwaway bucket ${BUCKET}"
$S3 delete-bucket "$BUCKET" || dp_fail "could not delete throwaway bucket ${BUCKET}"
for i in $(seq 1 12); do $S3 delete-bucket "${BUCKET}-m${i}" >/dev/null 2>&1 || true; done
dp_ok "throwaway bucket ${BUCKET} emptied and deleted"
