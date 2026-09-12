#!/usr/bin/env bash
# Runs whatever happened above, including after a crash mid-suite. Each check
# cleans up after itself when it finishes normally; this is the safety net for
# when one doesn't (a leftover openssl s_server, temp certs, tar fixtures).
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"

pkill -f "openssl s_server .*-accept 18443" 2>/dev/null || true
dp_run rm -rf "/tmp/${NS}-tls" "/tmp/${NS}-tar-src" "/tmp/${NS}-tar-dst" "/tmp/${NS}.tar.gz"

dp_ok "fixtures removed"
