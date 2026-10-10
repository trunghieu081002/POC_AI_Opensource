#!/usr/bin/env bash
# Every S3 bucket is its own SeaweedFS collection with its own volumes, and
# weed's defaults let the first bucket take every volume slot: the second one
# then cannot be written (HTTP 500 on PutObject). Found on a clean host when
# the bronze pipeline's bucket was written after this suite's own. A store that
# serves one bucket is not an object store for a pipeline fleet - so write to
# several, each in its own bucket, and read every one back.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
# shellcheck source=/dev/null
source "${INSTALL_DIR}/credentials.env"
export S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY
BASE="$(printf '%s' "$NS" | tr '_' '-' | tr '[:upper:]' '[:lower:]')"
S3="python3 ${INSTALL_DIR}/bin/s3.py"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

for i in $(seq 1 12); do
  b="${BASE}-m${i}"
  $S3 create-bucket "$b"
  printf 'bucket %s payload\n' "$i" > "$WORK/in"
  $S3 put "$b" "k/obj" "$WORK/in" || dp_fail "bucket ${i} of 12 could not be written - the store ran out of volume slots (volume_max / volume_growth_count)"
  $S3 get "$b" "k/obj" "$WORK/out"
  cmp -s "$WORK/in" "$WORK/out" || dp_fail "bucket ${i} returned different bytes"
done
for i in $(seq 1 12); do
  b="${BASE}-m${i}"
  $S3 delete "$b" "k/obj"
  $S3 delete-bucket "$b"
done
dp_ok "12 buckets each took a write and returned it (and were deleted again)"
