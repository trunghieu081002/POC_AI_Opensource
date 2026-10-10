#!/usr/bin/env bash
# The fundamental claim: what was written is what is read back, byte for byte -
# including a key with spaces and non-ASCII characters (bronze keys are built
# from pipeline and table names), and a payload large enough to be more than
# one network read.
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

head -c 3145728 /dev/urandom > "$WORK/big.bin"
printf 'ключ có dấu và khoảng trắng\n' > "$WORK/small.txt"

$S3 put "$BUCKET" "round trip/big payload.bin" "$WORK/big.bin"
$S3 put "$BUCKET" "round trip/ключ 1.txt" "$WORK/small.txt"
$S3 get "$BUCKET" "round trip/big payload.bin" "$WORK/big.out"
$S3 get "$BUCKET" "round trip/ключ 1.txt" "$WORK/small.out"

[ "$(sha256sum < "$WORK/big.bin")" = "$(sha256sum < "$WORK/big.out")" ] \
  || dp_fail "the 3 MiB object came back different from what was written"
cmp -s "$WORK/small.txt" "$WORK/small.out" \
  || dp_fail "the object with a non-ASCII key came back different from what was written"
dp_ok "3 MiB and a non-ASCII-keyed object round-trip byte for byte"
