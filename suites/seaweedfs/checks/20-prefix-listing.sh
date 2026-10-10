#!/usr/bin/env bash
# A bronze LOAD finds a batch's objects by listing a prefix, and teardown
# proves a namespace empty by listing it - so a listing that misses objects,
# includes a sibling prefix's, or stops at the first page would make either
# claim worthless. 1,100 keys cross a page boundary (1,000 per page).
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
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
echo x > "$WORK/o"

python3 - "$INSTALL_DIR/bin/s3.py" "$BUCKET" "$WORK/o" <<'PY'
import subprocess, sys, concurrent.futures as cf
s3, bucket, f = sys.argv[1:4]
def put(key):
    subprocess.run([sys.executable, s3, "put", bucket, key, f], check=True)
keys = [f"listing/a/{i:05d}" for i in range(1100)] + [f"listing/ab/{i:02d}" for i in range(5)]
with cf.ThreadPoolExecutor(16) as ex:
    list(ex.map(put, keys))
PY

A="$($S3 list "$BUCKET" "listing/a/" | grep -c .)"
AB="$($S3 list "$BUCKET" "listing/ab/" | grep -c .)"
ALL="$($S3 list "$BUCKET" "listing/" | grep -c .)"
[ "$A" = 1100 ] || dp_fail "prefix listing/a/ returned ${A} keys, expected 1100 (paging or prefix matching is wrong)"
[ "$AB" = 5 ] || dp_fail "prefix listing/ab/ returned ${AB} keys, expected 5"
[ "$ALL" = 1105 ] || dp_fail "prefix listing/ returned ${ALL} keys, expected 1105"
if $S3 list "$BUCKET" "listing/a/" | grep -q "listing/ab/"; then
  dp_fail "listing/a/ leaked a sibling prefix's key"
fi
dp_ok "prefix listings are exact across a page boundary (1100 / 5 / 1105)"
