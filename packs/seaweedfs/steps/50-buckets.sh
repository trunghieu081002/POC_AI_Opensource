#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
if [ "$DP_DRY_RUN" = "1" ]; then
  dp_info "would create bucket(s): $(dp_json_list buckets | tr '\n' ' ')"
  exit 0
fi
# shellcheck source=/dev/null
source "${INSTALL_DIR}/credentials.env"
export S3_ENDPOINT S3_ACCESS_KEY S3_SECRET_KEY

# the S3 gateway answers a moment after the port opens
for _ in $(seq 1 30); do
  if python3 "${INSTALL_DIR}/bin/s3.py" list-buckets >/dev/null 2>&1; then break; fi
  sleep 1
done

created=0
while read -r bucket; do
  [ -n "$bucket" ] || continue
  python3 "${INSTALL_DIR}/bin/s3.py" create-bucket "$bucket" || dp_fail "could not create bucket ${bucket}"
  python3 "${INSTALL_DIR}/bin/s3.py" head-bucket "$bucket" >/dev/null \
    || dp_fail "bucket ${bucket} was created but cannot be read back"
  created=$((created + 1))
done < <(dp_json_list buckets)
dp_ok "${created} bucket(s) ready"
