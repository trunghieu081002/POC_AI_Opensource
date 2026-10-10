#!/usr/bin/env bash
# Teardown of a validation purges a namespace and then lists it to prove it
# empty - that is only evidence if delete really removes (not just hides from
# one of reads/listings).
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
echo gone > "$WORK/o"

$S3 put "$BUCKET" "del/one" "$WORK/o"
$S3 list "$BUCKET" "del/" | grep -qx "del/one" || dp_fail "the object was not listed after it was written"
$S3 delete "$BUCKET" "del/one"
if $S3 get "$BUCKET" "del/one" "$WORK/back" 2>/dev/null; then
  dp_fail "a deleted object can still be read"
fi
[ -z "$($S3 list "$BUCKET" "del/")" ] || dp_fail "a deleted object is still listed"
dp_ok "a deleted object is gone from reads and from listings"
