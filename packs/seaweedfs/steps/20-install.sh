#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root
INSTALL_DIR="$(dp_param install_dir /opt/seaweedfs)"
VERSION="$(dp_param version 4.48)"

case "$(uname -m)" in
  x86_64|amd64)  ARCH=amd64; SHA="$(dp_param sha256_amd64)" ;;
  aarch64|arm64) ARCH=arm64; SHA="$(dp_param sha256_arm64)" ;;
  *) dp_fail "unsupported CPU architecture $(uname -m)" ;;
esac
[ -n "$SHA" ] || dp_fail "no sha256 pinned for ${ARCH} - refusing to install an unverified binary"

if ! dp_have curl && ! dp_have wget; then
  dp_pkg_install curl ca-certificates
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
URL="https://github.com/seaweedfs/seaweedfs/releases/download/${VERSION}/linux_${ARCH}.tar.gz"
dp_info "downloading SeaweedFS ${VERSION} (${ARCH})"
dp_fetch "$URL" "${WORK}/weed.tar.gz" "$SHA"
dp_run tar -xzf "${WORK}/weed.tar.gz" -C "$WORK"
if [ "$DP_DRY_RUN" != "1" ]; then
  [ -f "${WORK}/weed" ] || dp_fail "the tarball does not contain a 'weed' binary"
fi
dp_run install -m 0755 -o root -g root "${WORK}/weed" "${INSTALL_DIR}/bin/weed"
if [ "$DP_DRY_RUN" != "1" ]; then
  "${INSTALL_DIR}/bin/weed" version 2>&1 | grep -q "${VERSION}" \
    || dp_fail "installed weed does not report version ${VERSION}: $("${INSTALL_DIR}/bin/weed" version 2>&1 | head -2)"
fi
dp_ok "weed ${VERSION} installed at ${INSTALL_DIR}/bin/weed"
