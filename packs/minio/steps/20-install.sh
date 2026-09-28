#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"
# shellcheck source=/dev/null
source "${DP_PACK_ROOT:?}/mn-lib.sh"

dp_require_root
RELEASE="$(dp_param release)"
BIN="$(mn_bin)"
ARCH="$(mn_arch)"

if [ -z "$ARCH" ] && [ "$DP_DRY_RUN" != "1" ]; then
  dp_fail "unsupported CPU architecture '$(uname -m)' - MinIO ships amd64 and arm64 builds only"
fi
# Under --dry-run on an architecture this pack does not recognise, fall back
# to amd64 to preview the command that would run on a real supported host,
# the same reasoning dp_fetch itself applies to a missing curl/wget.
[ -n "$ARCH" ] || ARCH="amd64"

URL="$(mn_download_url "$ARCH" "$RELEASE")"
dp_fetch "$URL" "$BIN"
dp_run chmod 0755 "$BIN"
dp_run chown minio:minio "$BIN"

dp_ok "minio binary installed at ${BIN} (${ARCH}$( [ -n "$RELEASE" ] && echo ", ${RELEASE}" || echo ", latest stable" ))"
