#!/usr/bin/env bash
# Install the tooling that the rest of the library takes for granted.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

dp_require_root

dp_pkg_update

if dp_is_debian; then
  dp_pkg_install \
    ca-certificates curl wget gnupg \
    python3 python3-venv python3-pip \
    iproute2 psmisc procps \
    tar gzip xz-utils \
    locales tzdata \
    util-linux lsb-release \
    file
elif dp_is_rhel; then
  # iproute -> ss, psmisc -> fuser, procps-ng -> pgrep. Named differently here.
  dp_pkg_install \
    ca-certificates curl wget gnupg2 \
    python3 \
    iproute psmisc procps-ng \
    tar gzip xz \
    glibc-langpack-en tzdata \
    util-linux \
    file
else
  dp_fail "unsupported OS family: ${DP_OS_FAMILY:-unset}"
fi

# The CA bundle is only useful once it has been rebuilt into the trust store.
if dp_have update-ca-trust; then
  dp_run update-ca-trust extract
elif dp_have update-ca-certificates; then
  dp_run update-ca-certificates
fi

missing=""
for tool in python3 curl tar gzip ss fuser pgrep; do
  dp_have "$tool" || missing="${missing} ${tool}"
done
if [ "$DP_DRY_RUN" != "1" ]; then
  [ -z "$missing" ] || dp_fail "still missing after install:${missing}"
fi

dp_ok "base tooling installed"
