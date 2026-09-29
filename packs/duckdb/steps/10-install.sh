#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
VERSION="$(dp_param version 1.5.6)"
INSTALL_DIR="$(dp_param install_dir /opt/duckdb)"

case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) ARCH="" ;;
esac
if [ -z "$ARCH" ] && [ "$DP_DRY_RUN" != "1" ]; then
  dp_fail "unsupported CPU architecture '$(uname -m)' - duckdb ships amd64 and arm64 builds only"
fi
[ -n "$ARCH" ] || ARCH="amd64"   # preview a real command under --dry-run on an unrecognised arch

ZIP="/tmp/duckdb-${VERSION}-${ARCH}.zip"
dp_fetch "https://github.com/duckdb/duckdb/releases/download/v${VERSION}/duckdb_cli-linux-${ARCH}.zip" "$ZIP"
dp_run mkdir -p "$INSTALL_DIR/bin"
# python3's zipfile module rather than the `unzip` binary - base guarantees
# python3, not unzip, and this is the only file this pack ever needs out of
# the archive.
dp_run python3 -m zipfile -e "$ZIP" "$INSTALL_DIR/bin/"
dp_run chmod 0755 "$INSTALL_DIR/bin/duckdb"
dp_run rm -f "$ZIP"

dp_ok "duckdb ${VERSION} installed at ${INSTALL_DIR}/bin/duckdb (${ARCH})"
