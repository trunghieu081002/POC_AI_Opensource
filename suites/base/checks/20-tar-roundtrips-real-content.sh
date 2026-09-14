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

# Not diff/cmp: diffutils is not guaranteed present (missing by default on a
# minimal EL9 image, for one) and base itself does not install it - a plain
# shell string comparison needs nothing beyond bash itself.
ORIGINAL="$(cat "${SRC}/subdir/file.txt")"
EXTRACTED="$(cat "${DST}/subdir/file.txt")"
[ "$ORIGINAL" = "$EXTRACTED" ] \
  || dp_fail "extracted file content does not match the original — the round trip is lossy"

rm -rf "$SRC" "$DST" "$ARCHIVE"
dp_ok "tar round-tripped a file with quotes, spaces and a dollar sign byte-for-byte"
