#!/usr/bin/env bash
# Runs whatever happened above, including after a crash mid-suite.
#
# Guard every removal: a partial setup must still tear down cleanly. Use
# `if A; then B; fi`, never `A && B` — under `set -e` a false A exits the script
# and leaves the fixtures behind, which poisons the next run.
set -euo pipefail
# shellcheck source=/dev/null
source "${DP_LIB:?dp.sh not found}"

NS="${DP_TEST_NS:?}"

# A check that died may still hold a connection; close those before removing
# anything, or the removal fails and the next run inherits a dirty namespace.

dp_info "removing fixtures in ${NS}"
# if <the namespace exists>; then
#   dp_run <remove it>
# fi

dp_ok "fixtures removed"
