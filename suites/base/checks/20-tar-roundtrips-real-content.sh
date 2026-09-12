#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"
SRC="/tmp/${NS}-tar-src"
DST="/tmp/${NS}-tar-dst"
ARCHIVE="/tmp/${NS}.tar.gz"

rm -rf "$SRC" "$DST" "$ARCHIVE"
mkdir -p "${SRC}/subdir"
printf 'quote'"'"'s "double" $dollar\nsecond line\n' > "${SRC}/subdir/file.txt"

tar czf "$ARCHIVE" -C "$SRC" . || dp_fail "tar could not create an archive"
mkdir -p "$DST"
tar xzf "$ARCHIVE" -C "$DST" || dp_fail "tar could not extract the archive it just created"

diff -q "${SRC}/subdir/file.txt" "${DST}/subdir/file.txt" >/dev/null \
  || dp_fail "extracted file content does not match the original — the round trip is lossy"

rm -rf "$SRC" "$DST" "$ARCHIVE"
dp_ok "tar round-tripped a file with quotes, spaces and a dollar sign byte-for-byte"
