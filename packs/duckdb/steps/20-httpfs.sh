#!/usr/bin/env bash
# `duckdb -c "INSTALL httpfs"` downloads through DuckDB's own HTTP client,
# which - like google-auth's `requests` transport (see docs/deploy-log.md,
# the 2026-09-28 Google Sheets entry) - does not race IPv6/IPv4 the way
# `curl` does. Confirmed for real on this host: `curl -6` to
# extensions.duckdb.org hangs to timeout, `curl -4` answers; `duckdb -c
# "INSTALL httpfs"` hung until it gave up with "Connection timed out".
#
# Fetched directly with curl instead (which dp_fetch already prefers, and
# which succeeds because it races both families) into a shared,
# world-readable directory every invocation points `SET extension_directory`
# at - confirmed for real that DuckDB finds it there with zero network
# calls: `<dir>/v<version>/<platform>/httpfs.duckdb_extension`, the exact
# layout its own default per-user cache uses, just relocated somewhere every
# user (root, airflow, ...) can read without each needing their own copy.
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
[ -n "$ARCH" ] || ARCH="amd64"   # preview under --dry-run on an unrecognised arch

PLATFORM="linux_${ARCH}"
EXT_DIR="${INSTALL_DIR}/extensions/v${VERSION}/${PLATFORM}"
GZ="/tmp/duckdb-httpfs-${VERSION}-${ARCH}.duckdb_extension.gz"

dp_run mkdir -p "$EXT_DIR"
dp_fetch "http://extensions.duckdb.org/v${VERSION}/${PLATFORM}/httpfs.duckdb_extension.gz" "$GZ"
dp_run gzip -dfk "$GZ"
dp_run mv "${GZ%.gz}" "${EXT_DIR}/httpfs.duckdb_extension"
dp_run chmod -R a+rX "$(dp_param install_dir /opt/duckdb)/extensions"

dp_ok "httpfs extension fetched into ${EXT_DIR} - no network needed at query time"
